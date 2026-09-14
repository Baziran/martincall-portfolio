from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.engine.analyze import analyze_bars
from aef_terminal.features.vsa import vsa_facts
from aef_terminal.indicators.domain_facts import validate_indicator_fact_fields
from aef_terminal.indicators.modules.rounded_reversal import (
    RoundedReversalParams,
    _rounded_reversal_with_confirmed_context,
    build_params,
    rounded_reversal,
)
from aef_terminal.indicators.modules.rounded_reversal.fast import fast_rounded_reversal
from aef_terminal.indicators.modules.rounded_reversal.adaptive import (
    adaptive_rejection_summary,
)
from aef_terminal.indicators.registry import INDICATOR_REGISTRY
from aef_terminal.runtime.bar_quality import canonical_storage_bar_provenance
from tests.provider_payloads import (
    ibkr_stock_payload,
    instrument_with_bar_sessions,
)


_PROVENANCE = canonical_storage_bar_provenance(
    "ibkr",
    "instrument-es",
    "route-es",
)
_CONTEXT_IDENTITY = {
    "provider": "ibkr",
    "instrument_id": "instrument-es",
    "route_fingerprint": "route-es",
}


def _bar(
    index: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
    *,
    volume: float = 1000.0,
    closed: bool = True,
) -> Bar:
    return Bar(
        symbol="ES",
        ts=datetime(2026, 7, 1, 13, 30, tzinfo=UTC) + timedelta(minutes=index * 5),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe="5m",
        closed=closed,
    )


def _rounded_top_bars() -> list[Bar]:
    rows = [
        (100.0, 100.4, 99.6, 100.1),
        (100.1, 100.7, 99.9, 100.6),
        (100.6, 101.2, 100.4, 101.1),
        (101.1, 101.9, 100.9, 101.8),
        (101.8, 102.8, 101.6, 102.7),
        (102.7, 103.4, 102.4, 103.1),
        (103.1, 103.2, 101.3, 102.5),
        (102.5, 103.9, 102.3, 103.7),
        (103.7, 105.0, 103.5, 104.8),
        (104.8, 105.7, 104.6, 105.5),
        (105.5, 106.90, 105.2, 105.9),
        (105.9, 107.05, 105.6, 106.2),
        (106.2, 107.10, 105.8, 106.4),
        (106.4, 107.00, 105.9, 106.2),
        (106.2, 106.95, 105.7, 106.0),
        (106.0, 106.4, 105.1, 105.4),
        (105.4, 105.7, 103.8, 104.0),
        (104.0, 104.2, 100.8, 101.0),
        (100.6, 101.2, 100.4, 100.7),
        (101.1, 101.4, 100.3, 100.6),
    ]
    return [
        _bar(index, open_price, high, low, close)
        for index, (open_price, high, low, close) in enumerate(rows)
    ]


def _es_aug13_bars(
    timeframe: str,
    start: datetime,
    rows: list[tuple[float, float, float, float]],
) -> list[Bar]:
    minutes = 1 if timeframe == "1m" else 5
    return [
        Bar(
            symbol="ES",
            ts=start + timedelta(minutes=index * minutes),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=1_000.0,
            timeframe=timeframe,
            closed=True,
            provenance=_PROVENANCE,
        )
        for index, (open_price, high, low, close) in enumerate(rows)
    ]


def _es_aug13_fast_bars() -> list[Bar]:
    # Canonical IBKR provider bars from 2026-08-13 17:20-17:41 Asia/Jerusalem.
    return _es_aug13_bars(
        "1m",
        datetime(2026, 8, 13, 14, 20, tzinfo=UTC),
        [
            (7830.00, 7830.25, 7828.25, 7830.25),
            (7830.25, 7833.25, 7829.75, 7833.00),
            (7832.75, 7834.50, 7832.50, 7834.25),
            (7834.00, 7836.75, 7834.00, 7835.75),
            (7835.50, 7836.25, 7834.50, 7835.00),
            (7834.75, 7836.00, 7834.00, 7836.00),
            (7835.75, 7836.00, 7834.25, 7835.25),
            (7835.25, 7836.25, 7833.50, 7835.00),
            (7834.75, 7837.00, 7834.50, 7836.75),
            (7836.75, 7837.00, 7835.75, 7836.50),
            (7836.50, 7836.75, 7834.25, 7834.50),
            (7834.50, 7837.00, 7834.25, 7837.00),
            (7836.75, 7838.25, 7836.75, 7837.50),
            (7837.25, 7837.50, 7835.00, 7835.50),
            (7835.75, 7837.25, 7835.50, 7836.75),
            (7836.75, 7838.50, 7835.75, 7837.75),
            (7837.75, 7837.75, 7836.50, 7837.00),
            (7837.25, 7837.25, 7835.25, 7835.75),
            (7835.75, 7835.75, 7832.50, 7833.75),
            (7833.75, 7835.75, 7833.25, 7835.00),
            (7835.00, 7835.75, 7834.00, 7835.00),
            (7835.00, 7835.00, 7832.50, 7832.50),
        ],
    )


def _es_aug13_slow_bars() -> list[Bar]:
    # Minimal exact 5m prefix that confirms the same rounded top causally.
    return _es_aug13_bars(
        "5m",
        datetime(2026, 8, 13, 12, 30, tzinfo=UTC),
        [
            (7783.50, 7790.25, 7778.00, 7782.25),
            (7782.00, 7785.25, 7781.50, 7784.75),
            (7784.75, 7786.75, 7782.25, 7784.25),
            (7784.00, 7786.50, 7783.75, 7785.00),
            (7785.00, 7787.50, 7784.75, 7787.00),
            (7787.00, 7788.50, 7787.00, 7788.50),
            (7788.50, 7790.00, 7788.00, 7789.75),
            (7789.50, 7791.75, 7789.50, 7791.00),
            (7791.00, 7791.75, 7790.00, 7790.75),
            (7790.75, 7792.75, 7790.00, 7792.00),
            (7792.00, 7793.25, 7791.00, 7791.00),
            (7791.00, 7793.50, 7790.50, 7793.25),
            (7793.25, 7799.50, 7785.50, 7798.25),
            (7798.25, 7809.25, 7798.00, 7809.00),
            (7809.00, 7809.00, 7800.00, 7801.50),
            (7801.50, 7814.25, 7800.50, 7812.75),
            (7813.00, 7816.25, 7810.75, 7816.00),
            (7816.00, 7821.50, 7813.50, 7820.75),
            (7820.75, 7827.50, 7819.75, 7826.75),
            (7827.00, 7828.75, 7823.75, 7826.00),
            (7826.00, 7830.25, 7825.75, 7829.75),
            (7829.75, 7831.25, 7824.50, 7830.00),
            (7830.00, 7836.75, 7828.25, 7835.00),
            (7834.75, 7837.00, 7833.50, 7836.50),
            (7836.50, 7838.25, 7834.25, 7836.75),
            (7836.75, 7838.50, 7832.50, 7835.00),
            (7835.00, 7835.75, 7830.25, 7830.25),
            (7830.25, 7830.50, 7819.50, 7821.25),
        ],
    )


def _qualified_bars(bars: list[Bar] | None = None) -> list[Bar]:
    return [replace(bar, provenance=_PROVENANCE) for bar in (bars or _rounded_top_bars())]


def _independent_micro_rounded_top() -> list[Bar]:
    base = _rounded_top_bars()[0].ts
    return [
        replace(
            bar,
            ts=base + timedelta(minutes=index),
            timeframe="1m",
            provenance=_PROVENANCE,
        )
        for index, bar in enumerate(_rounded_top_bars())
    ]


def test_rounded_reversal_reports_typed_warmup() -> None:
    bars = [_bar(index, 100.0, 101.0, 99.0, 100.1) for index in range(5)]
    params = RoundedReversalParams()
    result = rounded_reversal(bars, tick_size=0.25, params=params)
    required_bars = max(params.min_rise_bars + params.pivot_len * 2 + 1, 10)

    assert result["latest"] is None
    assert result["availability"] == {
        "state": "blocked",
        "reason_code": "rounded_reversal_warmup",
        "required_bars": required_bars,
        "available_bars": 5,
    }


def test_rounded_reversal_scans_confirmed_1m_as_an_independent_axis() -> None:
    result = _rounded_reversal_with_confirmed_context(
        rounded_reversal,
        structure_bars=_qualified_bars(),
        structure_offset=0,
        tick_size=0.25,
        params=RoundedReversalParams(),
        confirmed_bar_context={"1m": _independent_micro_rounded_top()},
        confirmed_bar_quality={
            "1m": {
                "ok": True,
                "provider_complete": True,
            }
        },
        indicator_context={"identity": _CONTEXT_IDENTITY},
    )

    assert {item["source_tf"] for item in result["series"]} == {
        "5m",
        "1m",
    }
    assert {item["track"] for item in result["track_states"]} == {"fast", "slow"}
    assert result["version"] == "2.0-dual-track-adaptive"
    assert result["latest"]["source_tf"] == "5m" or result["latest"]["source_tf"] == "1m"
    assert result["settings"]["confirmed_bar_context"]["1m"]["admitted"] is True


@pytest.mark.parametrize(
    ("track_mode", "expected_tracks"),
    (("fast", {"fast"}), ("slow", {"slow"})),
)
def test_track_mode_selects_one_track_without_creating_another_source(
    track_mode: str,
    expected_tracks: set[str],
) -> None:
    result = _rounded_reversal_with_confirmed_context(
        rounded_reversal,
        structure_bars=_qualified_bars(),
        structure_offset=0,
        tick_size=0.25,
        params=RoundedReversalParams(track_mode=track_mode),
        confirmed_bar_context={"1m": _independent_micro_rounded_top()},
        confirmed_bar_quality={"1m": {"ok": True, "provider_complete": True}},
        indicator_context={"identity": _CONTEXT_IDENTITY},
    )

    assert {item["track"] for item in result["track_states"]} == expected_tracks
    assert {item["source"] for item in result["events"]} <= {"rounded_reversal"}


def test_fast_only_mode_reports_missing_confirmed_context_as_blocked() -> None:
    result = _rounded_reversal_with_confirmed_context(
        rounded_reversal,
        structure_bars=_qualified_bars(),
        structure_offset=0,
        tick_size=0.25,
        params=RoundedReversalParams(track_mode="fast"),
        confirmed_bar_context={},
        confirmed_bar_quality={},
        indicator_context={"identity": _CONTEXT_IDENTITY},
    )

    assert result["latest"] is None
    assert result["availability"]["reason_code"] == ("rounded_reversal_fast_context_absent")


def _micro_bars(
    *,
    apex_price: float = 107.10,
    rejection_offsets: set[int] | None = None,
    forming_offsets: set[int] | None = None,
    provenance=_PROVENANCE,
) -> list[Bar]:
    macro = _rounded_top_bars()
    start = macro[9].ts
    rejection_offsets = {8, 12, 17, 22, 27} if rejection_offsets is None else rejection_offsets
    forming_offsets = forming_offsets or set()
    rows: list[Bar] = []
    for offset in range(35):
        if offset in rejection_offsets:
            open_price = apex_price - 0.50
            close = apex_price - 0.62
            high = apex_price - 0.03
            low = apex_price - 0.75
        else:
            open_price = apex_price - 0.55
            close = apex_price - 0.47
            high = apex_price - 0.42
            low = apex_price - 0.75
        rows.append(
            Bar(
                symbol="ES",
                ts=start + timedelta(minutes=offset),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=100.0,
                timeframe="1m",
                closed=offset not in forming_offsets,
                provenance=provenance,
            )
        )
    return rows


def _range_quality(
    parent_bars: list[Bar],
    *,
    identity: dict[str, str] | None = None,
    covered: bool = True,
    snapshot_as_of: datetime | None = None,
) -> dict[str, dict[str, object]]:
    route = identity or _CONTEXT_IDENTITY
    parent_end = parent_bars[-1].ts + timedelta(minutes=5)
    coverage_start = min(parent_bars[0].ts, _micro_bars()[0].ts)
    coverage_end = parent_end if covered else coverage_start + timedelta(minutes=1)
    return {
        "1m": {
            "contract": "provider-mtf-range-quality-v2",
            **route,
            "timeframe": "1m",
            "provider_complete": covered,
            "window_start_ts": coverage_start.isoformat(),
            "coverage_end_exclusive": parent_end.isoformat(),
            "snapshot_as_of_ts": (snapshot_as_of or parent_end).isoformat(),
            "eligible_ranges": [
                {
                    "from": coverage_start.isoformat(),
                    "to": coverage_end.isoformat(),
                }
            ],
            "eligible_range_count": 1,
        }
    }


def _state(bars: list[Bar]) -> str:
    result = rounded_reversal(bars, tick_size=0.25)
    latest = result["latest"]
    return latest["state"] if latest else "IDLE"


def test_rounded_top_lifecycle_is_prefix_causal() -> None:
    bars = _rounded_top_bars()

    assert _state(bars[:14]) == "IDLE"
    assert _state(bars[:15]) == "ARMED"
    assert _state(bars[:16]) == "ARMED"
    assert _state(bars[:17]) == "CONFIRMED"
    assert _state(bars[:18]) == "CONFIRMED"

    armed = rounded_reversal(bars[:15], tick_size=0.25)
    confirmed = rounded_reversal(bars[:17], tick_size=0.25)
    retest_bar = _bar(20, 104.5, 104.7, 103.7, 104.0)
    retest = rounded_reversal([*bars, retest_bar], tick_size=0.25)
    assert armed["events"][0]["code"] == "rounded_top_slow_armed"
    assert confirmed["events"][0]["code"] == "rounded_top_slow_neckline_break"
    assert retest["latest"]["state"] == "RETEST_CONFIRMED"
    assert retest["events"][0]["code"] == "rounded_top_slow_retest_rejection"


def test_es_aug13_fast_reanchors_at_the_high_then_triggers_on_closed_structure() -> None:
    bars = _es_aug13_fast_bars()
    params = RoundedReversalParams()

    initial = fast_rounded_reversal(bars[:13], tick_size=0.25, params=params)["latest"]
    reanchored = fast_rounded_reversal(bars[:16], tick_size=0.25, params=params)["latest"]
    triggered_result = fast_rounded_reversal(bars[:19], tick_size=0.25, params=params)
    triggered = triggered_result["latest"]
    retested = fast_rounded_reversal(bars, tick_size=0.25, params=params)["latest"]

    assert initial["state"] == "ARMED"
    assert initial["available_at_ts"] == "2026-08-13T14:33:00+00:00"
    assert initial["levels"]["apex"] == 7838.25
    assert reanchored["state"] == "ARMED"
    assert reanchored["levels"]["apex"] == 7838.5
    assert reanchored["levels"]["trigger"] == 7834.25
    assert reanchored["reanchored_from_pattern_id"] == (
        "rounded-top-fast:2026-08-13T14:32:00+00:00:7838.250000"
    )
    assert reanchored["available_at_ts"] == "2026-08-13T14:36:00+00:00"
    assert triggered["state"] == "TRIGGERED"
    assert triggered["transition_ts"] == "2026-08-13T14:38:00+00:00"
    assert triggered["available_at_ts"] == "2026-08-13T14:39:00+00:00"
    assert triggered_result["events"][0]["track"] == "fast"
    assert triggered_result["events"][0]["code"] == "rounded_top_fast_triggered"
    assert retested["state"] == "RETEST_CONFIRMED"
    assert retested["transition_ts"] == "2026-08-13T14:41:00+00:00"


def test_fast_track_is_stable_across_equal_highs_and_ignores_provisional_tail() -> None:
    bars = _es_aug13_fast_bars()
    params = RoundedReversalParams()

    at_first_high = fast_rounded_reversal(bars[:10], tick_size=0.25, params=params)
    after_equal_high = fast_rounded_reversal(bars[:12], tick_size=0.25, params=params)
    assert at_first_high["latest"]["pattern_id"] == after_equal_high["latest"]["pattern_id"]

    baseline = fast_rounded_reversal(bars[:19], tick_size=0.25, params=params)
    with_provisional = fast_rounded_reversal(
        [
            *bars[:19],
            replace(
                bars[19],
                high=8_000.0,
                close=8_000.0,
                closed=False,
                state=None,
            ),
        ],
        tick_size=0.25,
        params=params,
    )
    assert with_provisional == baseline


def test_fast_structure_break_cannot_skip_the_armed_gate() -> None:
    result = fast_rounded_reversal(
        _es_aug13_fast_bars(),
        tick_size=0.25,
        params=RoundedReversalParams(fast_target_mass=2.5),
    )

    assert result["latest"]["state"] == "WATCH"
    assert result["latest"]["armed_index"] is None
    assert result["latest"]["breakdown_index"] is None


def test_es_aug13_slow_uses_adaptive_auction_and_local_neckline() -> None:
    bars = _es_aug13_slow_bars()

    before_confirmation = rounded_reversal(bars[:-1], tick_size=0.25)
    confirmed = rounded_reversal(bars, tick_size=0.25)["latest"]

    assert before_confirmation["latest"] is None
    assert confirmed["track"] == "slow"
    assert confirmed["state"] == "CONFIRMED"
    assert confirmed["anchors"]["apex"]["ts"] == "2026-08-13T14:35:00+00:00"
    assert confirmed["anchors"]["apex"]["price"] == 7838.5
    assert confirmed["anchors"]["local_neckline"]["ts"] == ("2026-08-13T14:20:00+00:00")
    assert confirmed["available_at_ts"] == "2026-08-13T14:50:00+00:00"
    assert confirmed["rejection_cluster"]["support_bar_count"] == 2
    assert confirmed["rejection_cluster"]["evidence_mass"] == pytest.approx(1.8821)
    assert confirmed["levels"]["origin_base"] == 7785.5
    assert confirmed["levels"]["local_neckline"] == 7828.25
    assert confirmed["levels"]["theoretical_target"] == 7818.0


def test_adaptive_rejection_degrades_continuously_for_one_tick_near_miss() -> None:
    exact = Bar("ES", datetime(2026, 8, 13, 14, 30, tzinfo=UTC), 100, 101, 99, 99.5, 1, "1m")
    one_tick_short = replace(exact, high=100.75)

    def score(bar: Bar) -> float:
        summary = adaptive_rejection_summary(
            [bar],
            atr_values=[2.5],
            apex_price=101.5,
            apex_atr=2.5,
            tick_size=0.25,
            target_wick_share=0.20,
            target_wick_atr=0.10,
            target_close_pos=0.60,
            level_tolerance_atr=0.20,
            level_tolerance_ticks=2,
            target_mass=1.0,
            max_scored_bars=1,
            cluster_threshold=0.40,
            strong_bar_threshold=0.40,
            min_support_bars=1,
            source_timeframe="1m",
        )
        return float(summary["observations"][0]["score"])

    assert score(exact) == 1.0
    assert score(one_tick_short) == pytest.approx(0.5)


def test_confirmation_zone_reuses_option_reversal_animated_border_contract() -> None:
    result = rounded_reversal(_rounded_top_bars()[:17], tick_size=0.25)

    zone = next(item for item in result["overlays"] if item["role"] == "rounded_reversal_slow_zone")
    assert zone["type"] == "box"
    assert zone["tone"] == "negative"
    assert zone["style"] == "dashed"
    assert zone["width"] == 1.6
    assert zone["opacity"] == 0.0
    assert zone["border_opacity"] == 0.84
    assert zone["animated_border"] is True
    assert "fill_mode" not in zone
    assert "bg" not in zone
    assert "border" not in zone

    after_transition = rounded_reversal(
        [
            *_rounded_top_bars()[:18],
            _bar(18, 100.9, 101.0, 99.9, 100.2),
        ],
        tick_size=0.25,
    )
    static_zone = next(
        item
        for item in after_transition["overlays"]
        if item["role"] == "rounded_reversal_slow_zone"
    )
    assert static_zone["animated_border"] is False


def test_forming_bar_cannot_confirm_breakdown() -> None:
    bars = _rounded_top_bars()[:17]
    forming_break = _bar(
        17,
        104.0,
        104.2,
        100.2,
        100.4,
        closed=False,
    )

    baseline = rounded_reversal(bars, tick_size=0.25)
    with_forming = rounded_reversal(
        [*bars, forming_break],
        tick_size=0.25,
    )
    assert with_forming == baseline
    assert with_forming["latest"]["state"] == "CONFIRMED"


def test_internal_pending_parent_slot_blocks_every_later_bar() -> None:
    bars = _rounded_top_bars()
    pending = replace(bars[16], closed=False, state=None)

    with_pending_gap = rounded_reversal(
        [*bars[:16], pending, *bars[17:20]],
        tick_size=0.25,
    )
    causal_prefix = rounded_reversal(bars[:16], tick_size=0.25)

    assert with_pending_gap == causal_prefix
    assert with_pending_gap["latest"]["state"] == "ARMED"


def test_compressed_parent_gap_cannot_create_a_pattern_across_the_gap() -> None:
    bars = _rounded_top_bars()
    compressed = [*bars[:16], *bars[17:20]]

    result = rounded_reversal(compressed, tick_size=0.25)

    assert result["latest"] is None
    assert result["events"] == []
    assert result["overlays"] == []


def test_rejection_cluster_without_decelerating_shape_is_not_a_pattern() -> None:
    rows: list[Bar] = []
    for index in range(18):
        price = 100.0 + index * 0.7
        high = price + (1.0 if index in {10, 11, 12, 13} else 0.3)
        rows.append(
            _bar(
                index,
                price - 0.1,
                high,
                price - 0.4,
                price,
            )
        )
    rows.extend(
        [
            _bar(18, 112.0, 112.2, 110.5, 111.0),
            _bar(19, 111.0, 111.2, 108.0, 108.4),
        ]
    )

    result = rounded_reversal(rows, tick_size=0.25)
    assert result["latest"] is None
    assert result["events"] == []
    assert result["overlays"] == []


def test_shape_without_rejection_cluster_never_retroactively_confirms() -> None:
    bars = _rounded_top_bars()
    muted: list[Bar] = []
    for index, bar in enumerate(bars):
        if 9 <= index <= 15:
            top = max(bar.open, bar.close) + 0.12
            muted.append(
                _bar(
                    index,
                    bar.open,
                    top,
                    min(bar.low, bar.open, bar.close),
                    bar.close,
                )
            )
        else:
            muted.append(bar)

    result = rounded_reversal(muted, tick_size=0.25)
    assert result["latest"] is not None
    assert result["latest"]["rejection_cluster"]["qualified"] is False
    assert result["latest"]["state"] == "MISSED"
    assert result["overlays"] == []


def test_pattern_identity_and_frozen_levels_survive_appended_confirmed_bars() -> None:
    bars = _rounded_top_bars()
    armed = rounded_reversal(bars[:16], tick_size=0.25)["latest"]
    confirmed = rounded_reversal(bars[:18], tick_size=0.25)["latest"]
    retest = rounded_reversal(bars[:20], tick_size=0.25)["latest"]

    assert armed["pattern_id"] == confirmed["pattern_id"] == retest["pattern_id"]
    assert armed["anchors"] == confirmed["anchors"] == retest["anchors"]
    assert armed["levels"] == confirmed["levels"] == retest["levels"]


def test_close_above_apex_invalidates_and_later_drop_does_not_revive() -> None:
    bars = _rounded_top_bars()[:16]
    invalidation = _bar(16, 106.8, 107.8, 106.6, 107.6)
    later_drop = _bar(17, 107.4, 107.5, 100.0, 100.5)

    invalidated = rounded_reversal(
        [*bars, invalidation],
        tick_size=0.25,
    )
    still_invalid = rounded_reversal(
        [*bars, invalidation, later_drop],
        tick_size=0.25,
    )
    assert invalidated["latest"]["state"] == "INVALIDATED"
    assert still_invalid["latest"]["state"] == "INVALIDATED"
    assert still_invalid["overlays"] == []


def test_payload_uses_typed_facts_and_never_emits_a_trade_signal() -> None:
    result = rounded_reversal(_rounded_top_bars()[:17], tick_size=0.25)

    validate_indicator_fact_fields(result["latest"])
    validate_indicator_fact_fields(result["events"][0])
    assert result["events"][0]["track"] == "slow"
    assert result["events"][0]["adaptive_thresholds"]
    for overlay in result["overlays"]:
        validate_indicator_fact_fields(overlay)
    assert result["signals"] == []
    assert "trade_plan" not in result["latest"]
    assert "tooltip" not in result["latest"]
    assert "message" not in result["latest"]
    assert result["latest"]["levels"]["theoretical_target"] < result["latest"]["levels"]["neckline"]


def test_volume_does_not_change_v2_structure_state() -> None:
    bars = _rounded_top_bars()[:18]
    altered = [
        _bar(
            index,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            volume=0.0 if index % 2 else 1_000_000.0,
        )
        for index, bar in enumerate(bars)
    ]

    original = rounded_reversal(bars, tick_size=0.25)
    changed = rounded_reversal(altered, tick_size=0.25)
    assert original["latest"]["state"] == changed["latest"]["state"]
    assert original["latest"]["pattern_id"] == changed["latest"]["pattern_id"]
    assert original["latest"]["levels"] == changed["latest"]["levels"]


def test_complete_one_minute_cluster_is_the_effective_rejection_source() -> None:
    bars = _qualified_bars()
    result = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={"1m": _micro_bars()},
        mtf_quality=_range_quality(bars[:18]),
    )

    latest = result["latest"]
    micro = latest["micro_rejection_cluster"]
    assert latest["state"] == "CONFIRMED"
    assert latest["confirmation"]["mode"] == "mtf_preferred"
    assert latest["confirmation"]["effective_rejection_source"] == "1m"
    assert micro["availability_state"] == "qualified"
    assert micro["qualified"] is True
    assert micro["count"] == 5
    assert micro["ready_parent_index"] == 11
    assert micro["ready_ts"] == (_rounded_top_bars()[11].ts + timedelta(minutes=3)).isoformat()
    assert latest["anchors"]["apex"]["price"] == 107.1
    assert latest["levels"]["structural_invalidation"] > 107.1
    zone = next(item for item in result["overlays"] if item["role"] == "rounded_reversal_slow_zone")
    assert zone["animated_border"] is False
    assert zone["top"] == latest["levels"]["effective_rejection_zone_top"]


def test_one_minute_rejections_can_rescue_missing_macro_wicks() -> None:
    bars = _qualified_bars()
    muted = [
        (
            replace(
                bar,
                high=max(bar.open, bar.close) + 0.12,
            )
            if 9 <= index <= 15
            else bar
        )
        for index, bar in enumerate(bars)
    ]
    macro_only = rounded_reversal(muted[:18], tick_size=0.25)
    apex = macro_only["latest"]["anchors"]["apex"]["price"]
    linked = rounded_reversal(
        muted[:18],
        tick_size=0.25,
        mtf_context={"1m": _micro_bars(apex_price=apex)},
        mtf_quality=_range_quality(muted[:18]),
    )

    assert macro_only["latest"]["rejection_cluster"]["qualified"] is False
    assert macro_only["latest"]["state"] == "MISSED"
    assert linked["latest"]["rejection_cluster"]["qualified"] is False
    assert linked["latest"]["micro_rejection_cluster"]["qualified"] is True
    assert linked["latest"]["state"] == "CONFIRMED"
    assert linked["latest"]["pattern_id"] == macro_only["latest"]["pattern_id"]
    assert linked["latest"]["levels"]["neckline"] == macro_only["latest"]["levels"]["neckline"]
    assert (
        linked["latest"]["levels"]["structural_invalidation"]
        == macro_only["latest"]["levels"]["structural_invalidation"]
    )


def test_reliable_one_minute_negative_evidence_does_not_fallback_to_macro() -> None:
    bars = _qualified_bars()
    result = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={
            "1m": _micro_bars(rejection_offsets={17}),
        },
        mtf_quality=_range_quality(bars[:18]),
    )

    latest = result["latest"]
    micro = latest["micro_rejection_cluster"]
    assert latest["rejection_cluster"]["qualified"] is True
    assert micro["availability_state"] == "not_qualified"
    assert micro["qualified"] is False
    assert latest["confirmation"]["effective_rejection_source"] == "1m"
    assert latest["state"] == "MISSED"
    assert result["overlays"] == []


def test_full_future_micro_context_is_trimmed_for_every_parent_prefix() -> None:
    bars = _qualified_bars()
    micro = _micro_bars()
    prefix = bars[:15]
    parent_close = prefix[-1].ts + timedelta(minutes=5)
    explicit_prefix = [bar for bar in micro if bar.ts + timedelta(minutes=1) <= parent_close]

    with_full_context = rounded_reversal(
        prefix,
        tick_size=0.25,
        mtf_context={"1m": micro},
        mtf_quality=_range_quality(prefix),
    )
    with_trimmed_context = rounded_reversal(
        prefix,
        tick_size=0.25,
        mtf_context={"1m": explicit_prefix},
        mtf_quality=_range_quality(prefix),
    )
    assert with_full_context == with_trimmed_context
    assert with_full_context["latest"]["confirmation"]["micro"]["availability_state"] == "qualified"


def test_pending_micro_slot_blocks_instead_of_becoming_negative_evidence() -> None:
    bars = _qualified_bars()
    with_forming = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={
            "1m": _micro_bars(
                rejection_offsets={8, 12, 17, 22},
                forming_offsets={22},
            )
        },
    )
    assert with_forming["latest"]["micro_rejection_cluster"]["availability_state"] == "collecting"
    assert with_forming["latest"]["confirmation_pending"] is True
    assert with_forming["latest"]["state"] == "FORMING"

    with_late_pending = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={
            "1m": _micro_bars(
                rejection_offsets={8, 12, 17, 22},
                forming_offsets={30},
            )
        },
    )
    late_pending_micro = with_late_pending["latest"]["micro_rejection_cluster"]
    assert late_pending_micro["observed_qualified"] is True
    assert late_pending_micro["qualified"] is False
    assert late_pending_micro["availability_state"] == "collecting"
    assert with_late_pending["latest"]["state"] == "FORMING"

    resolved = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={
            "1m": _micro_bars(
                rejection_offsets={8, 12, 17, 22},
            )
        },
        mtf_quality=_range_quality(bars[:18]),
    )
    assert resolved["latest"]["state"] == "CONFIRMED"
    assert resolved["latest"]["confirmation_pending"] is False


def test_exact_covered_micro_range_admits_omitted_timestamp() -> None:
    bars = _qualified_bars()
    micro = _micro_bars()
    omitted_ts = micro[5].ts
    result = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={
            "1m": [bar for bar in micro if bar.ts != omitted_ts],
        },
        mtf_quality=_range_quality(bars[:18]),
    )

    micro_result = result["latest"]["micro_rejection_cluster"]
    assert micro_result["availability_state"] == "qualified"
    assert micro_result["quality"]["input_admitted"] is True
    assert micro_result["quality"]["provider_complete"] is True
    assert result["latest"]["state"] == "CONFIRMED"


def test_partial_micro_coverage_does_not_block_confirmed_input() -> None:
    bars = _qualified_bars()
    result = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={"1m": _micro_bars()},
        mtf_quality=_range_quality(bars[:18], covered=False),
    )

    micro_result = result["latest"]["micro_rejection_cluster"]
    assert micro_result["availability_state"] == "qualified"
    assert micro_result["reason_code"] == "micro_rejections_qualified"
    assert micro_result["quality"]["input_admitted"] is True
    assert micro_result["quality"]["provider_complete"] is False
    assert result["latest"]["state"] == "CONFIRMED"


def test_future_quality_snapshot_cannot_change_a_parent_prefix() -> None:
    bars = _qualified_bars()
    prefix = bars[:15]
    micro = _micro_bars()
    baseline = rounded_reversal(
        prefix,
        tick_size=0.25,
        mtf_context={"1m": micro},
    )
    future_snapshot = micro[-1].ts + timedelta(minutes=1)
    with_future_quality = rounded_reversal(
        prefix,
        tick_size=0.25,
        mtf_context={"1m": micro},
        mtf_quality=_range_quality(
            prefix,
            snapshot_as_of=future_snapshot,
        ),
    )

    assert with_future_quality["latest"]["state"] == baseline["latest"]["state"]
    assert with_future_quality["latest"]["levels"] == baseline["latest"]["levels"]
    assert (
        with_future_quality["latest"]["micro_rejection_cluster"]["quality"]["provider_complete"]
        is False
    )


def test_wrong_route_cannot_authorize_mtf() -> None:
    bars = _qualified_bars()

    foreign = canonical_storage_bar_provenance(
        "ibkr",
        "instrument-es",
        "foreign-route",
    )
    wrong_route = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={"1m": _micro_bars(provenance=foreign)},
    )
    solo = rounded_reversal(bars[:18], tick_size=0.25)
    assert wrong_route["latest"]["confirmation"]["micro"]["reason_code"] == "micro_route_mismatch"
    assert wrong_route["latest"]["confirmation"]["effective_rejection_source"] == "macro"
    assert wrong_route["latest"]["state"] == solo["latest"]["state"]
    assert wrong_route["latest"]["levels"] == solo["latest"]["levels"]


def test_future_foreign_micro_route_cannot_change_a_parent_prefix() -> None:
    bars = _qualified_bars()
    micro = _micro_bars()
    foreign = canonical_storage_bar_provenance(
        "ibkr",
        "instrument-es",
        "future-route",
    )
    future_foreign = [
        (
            replace(
                bar,
                timeframe="2m",
                provenance=foreign,
            )
            if offset >= 30
            else bar
        )
        for offset, bar in enumerate(micro)
    ]

    baseline = rounded_reversal(
        bars[:15],
        tick_size=0.25,
        mtf_context={"1m": micro},
    )
    with_foreign_future = rounded_reversal(
        bars[:15],
        tick_size=0.25,
        mtf_context={"1m": future_foreign},
    )

    assert with_foreign_future == baseline


def test_shorter_confirmed_mtf_series_does_not_wait_for_coverage_receipt() -> None:
    bars = _qualified_bars()
    incomplete_negative = _micro_bars(rejection_offsets={17})[:25]
    shorter_confirmed = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={"1m": incomplete_negative},
    )
    complete_negative = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={
            "1m": _micro_bars(rejection_offsets={17}),
        },
        mtf_quality=_range_quality(bars[:18]),
    )

    assert shorter_confirmed["latest"]["confirmation_pending"] is False
    assert shorter_confirmed["latest"]["state"] == "MISSED"
    assert (
        shorter_confirmed["latest"]["micro_rejection_cluster"]["availability_state"]
        == "not_qualified"
    )
    assert complete_negative["latest"]["confirmation_pending"] is False
    assert complete_negative["latest"]["state"] == "MISSED"


def test_vsa_and_absorption_are_typed_advisory_links_only() -> None:
    bars = _qualified_bars()
    solo = rounded_reversal(bars[:17], tick_size=0.25)
    vsa_rows = [
        (
            replace(
                fact,
                primary_code="UPTHRUST",
                code="UPTHRUST",
                upthrust=True,
                absorption_role="range",
            )
            if fact.index == 14
            else replace(
                fact,
                primary_code="FUEL_UP",
                code="FUEL_UP",
                buy_fuel=True,
                absorption_role="range",
            )
            if fact.index == 19
            else fact
        )
        for fact in vsa_facts(bars)
    ]
    absorption_context = {
        "events": [
            {
                "ts": bars[14].ts.isoformat(),
                "attack_ts": bars[13].ts.isoformat(),
                "state": "FAILED_HIGH",
                "code": "ABS_HIGH",
                "direction": "short",
                "level": 106.9,
                "pressure_source": "candle_proxy",
                "signal": {"confirmed": True},
                "tooltip": "this display string must never be parsed",
            }
        ]
    }
    linked = rounded_reversal(
        bars[:17],
        tick_size=0.25,
        indicator_context={
            "identity": _CONTEXT_IDENTITY,
            "vsa_facts": vsa_rows,
            "absorption_trap": absorption_context,
        },
    )

    latest = linked["latest"]
    codes = {item["code"] for item in latest["linked_context"]["evidence"]}
    assert codes == {"vsa_upthrust", "absorption_high_confirmed"}
    assert latest["linked_context"]["mode"] == "linked"
    assert latest["linked_context"]["authoritative_for_lifecycle"] is False
    assert latest["linked_context"]["calibrated"] is False
    assert latest["linked_context"]["identity_verified"] is True
    assert latest["linked_context"]["contextual_score"] > latest["score"]
    assert latest["score"] == solo["latest"]["score"]
    assert latest["state"] == solo["latest"]["state"]
    assert latest["pattern_id"] == solo["latest"]["pattern_id"]
    assert latest["levels"] == solo["latest"]["levels"]
    validate_indicator_fact_fields(latest)
    validate_indicator_fact_fields(linked["events"][0])
    for overlay in linked["overlays"]:
        validate_indicator_fact_fields(overlay)
    assert all(
        item.get("evidence_ts") != bars[19].ts.isoformat()
        for item in latest["linked_context"]["evidence"]
    )


def test_advisory_tick_flow_never_gates_or_claims_absorption() -> None:
    bars = _qualified_bars()
    solo = rounded_reversal(bars[:18], tick_size=0.25)
    tick_context = {
        "provider": "ibkr",
        "instrument_id": "instrument-es",
        "route_fingerprint": "route-es",
        "classification_confidence": "advisory",
        "decision_eligible": False,
        "delta": [
            {
                "ts": bars[14].ts.isoformat(),
                "total_volume": 1000,
                "net_delta": 800,
            }
        ],
    }
    linked = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        indicator_context={
            "identity": _CONTEXT_IDENTITY,
            "tick_flow": tick_context,
        },
    )

    latest = linked["latest"]
    assert latest["linked_context"]["links"]["tick_flow"] == {
        "state": "advisory_only",
        "reason_code": "tick_flow_not_decision_eligible",
    }
    assert latest["linked_context"]["evidence"] == []
    assert latest["state"] == solo["latest"]["state"]
    assert latest["score"] == solo["latest"]["score"]
    assert latest["levels"] == solo["latest"]["levels"]


def test_eligible_tick_flow_is_bounded_to_qualified_tail_minutes() -> None:
    bars = _qualified_bars()
    micro = _micro_bars()
    outside_tail = micro[5].ts
    inside_tail = micro[8].ts
    linked = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        mtf_context={"1m": micro},
        mtf_quality=_range_quality(bars[:18]),
        indicator_context={
            "identity": _CONTEXT_IDENTITY,
            "tick_flow": {
                "provider": "ibkr",
                "instrument_id": "instrument-es",
                "route_fingerprint": "route-es",
                "classification_confidence": "confirmed",
                "decision_eligible": True,
                "delta": [
                    {
                        "ts": outside_tail.isoformat(),
                        "total_volume": 100_000,
                        "net_delta": 100_000,
                    },
                    {
                        "ts": inside_tail.isoformat(),
                        "total_volume": 100,
                        "net_delta": -80,
                    },
                ],
            },
        },
    )

    tick_link = linked["latest"]["linked_context"]["links"]["tick_flow"]
    assert tick_link["row_count"] == 1
    assert tick_link["delta_ratio"] == -0.8
    assert tick_link["reason_code"] == ("no_aligned_positive_delta_rejection")
    assert linked["latest"]["linked_context"]["evidence"] == []


def test_linked_context_requires_exact_parent_route_envelope() -> None:
    bars = _qualified_bars()
    typed_vsa = [
        (
            replace(
                fact,
                primary_code="UPTHRUST",
                code="UPTHRUST",
                upthrust=True,
            )
            if fact.index == 14
            else fact
        )
        for fact in vsa_facts(bars[:18])
    ]
    result = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        indicator_context={
            "identity": {
                **_CONTEXT_IDENTITY,
                "route_fingerprint": "foreign-route",
            },
            "vsa_facts": typed_vsa,
        },
    )

    linked = result["latest"]["linked_context"]
    assert linked["mode"] == "solo"
    assert linked["identity_verified"] is False
    assert linked["evidence"] == []
    assert linked["links"]["vsa_volume"]["reason_code"] == ("context_route_unverified")


def test_tick_payload_route_must_match_the_shared_context_envelope() -> None:
    bars = _qualified_bars()
    result = rounded_reversal(
        bars[:18],
        tick_size=0.25,
        indicator_context={
            "identity": _CONTEXT_IDENTITY,
            "tick_flow": {
                "provider": "ibkr",
                "instrument_id": "instrument-es",
                "route_fingerprint": "foreign-route",
                "decision_eligible": True,
                "delta": [
                    {
                        "ts": bars[14].ts.isoformat(),
                        "total_volume": 100,
                        "net_delta": 90,
                    }
                ],
            },
        },
    )

    linked = result["latest"]["linked_context"]
    assert linked["evidence"] == []
    assert linked["links"]["tick_flow"] == {
        "state": "ignored",
        "reason_code": "tick_flow_route_unverified",
    }


def test_registered_structure_pipeline_emits_animated_zone_without_candidate() -> None:
    raw_bars = _rounded_top_bars()[:17]
    instrument = instrument_with_bar_sessions(
        ibkr_stock_payload("SPY"),
        raw_bars,
    )
    route = route_instrument(instrument, expected_source="ibkr")
    provenance = canonical_storage_bar_provenance(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    bars = [replace(bar, symbol="SPY", provenance=provenance) for bar in raw_bars]
    micro = [replace(bar, symbol="SPY", provenance=provenance) for bar in _micro_bars()]
    indicator_params = {
        indicator_id: {"enabled": indicator_id == "rounded_reversal"}
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.pipeline_stage != "ui"
    }
    indicator_params["global_defaults"] = {}

    snapshot = analyze_bars(
        bars,
        instrument=instrument,
        indicator_params=indicator_params,
        mtf_context={"1m": micro},
        mtf_quality=_range_quality(
            bars,
            identity={
                "provider": route.provider,
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
            },
        ),
        analysis_as_of_utc=bars[-1].ts + timedelta(minutes=5),
    )

    indicator = snapshot["indicators"]["rounded_reversal"]
    assert indicator["latest"]["state"] == "CONFIRMED"
    assert indicator["signals"] == []
    zone = next(
        item for item in indicator["overlays"] if item["role"] == "rounded_reversal_slow_zone"
    )
    assert zone["animated_border"] is True
    assert all(item.get("source") != "rounded_reversal" for item in snapshot["candidates"])


@pytest.mark.parametrize(
    ("covered", "quality_route"),
    (
        (True, "exact"),
        (False, "exact"),
        (True, "foreign"),
    ),
)
def test_registered_structure_pipeline_treats_receipt_quality_as_diagnostic(
    covered: bool,
    quality_route: str,
) -> None:
    raw_bars = _rounded_top_bars()[:18]
    instrument = instrument_with_bar_sessions(
        ibkr_stock_payload("SPY"),
        raw_bars,
    )
    route = route_instrument(instrument, expected_source="ibkr")
    provenance = canonical_storage_bar_provenance(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    bars = [replace(bar, symbol="SPY", provenance=provenance) for bar in raw_bars]
    complete_micro = [replace(bar, symbol="SPY", provenance=provenance) for bar in _micro_bars()]
    absent_ts = complete_micro[5].ts
    micro = [bar for bar in complete_micro if bar.ts != absent_ts]
    parent_end = bars[-1].ts + timedelta(minutes=5)
    indicator_params = {
        indicator_id: {"enabled": indicator_id == "rounded_reversal"}
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.pipeline_stage != "ui"
    }
    indicator_params["global_defaults"] = {}
    authority = _range_quality(
        bars,
        identity={
            "provider": route.provider,
            "instrument_id": route.instrument_id,
            "route_fingerprint": (
                route.fingerprint if quality_route == "exact" else "foreign-route"
            ),
        },
        covered=covered,
    )["1m"]

    snapshot = analyze_bars(
        bars,
        instrument=instrument,
        indicator_params=indicator_params,
        mtf_context={"1m": micro},
        mtf_quality={"1m": authority},
        analysis_as_of_utc=parent_end,
    )

    indicator = snapshot["indicators"]["rounded_reversal"]
    micro_result = indicator["latest"]["micro_rejection_cluster"]
    assert indicator["latest"]["state"] == "CONFIRMED"
    assert micro_result["quality"]["input_admitted"] is True
    assert micro_result["quality"]["provider_complete"] is (covered and quality_route == "exact")
    assert micro_result["quality"]["quality_scope_verified"] is (quality_route == "exact")
    assert snapshot["meta"]["security_context"]["1m"]["quality"]["provider_complete"] is covered
    assert "eligible_ranges" not in snapshot["meta"]["security_context"]["1m"]["quality"]


def test_due_micro_finality_waits_only_the_dependent_indicator() -> None:
    raw_bars = _rounded_top_bars()[:18]
    instrument = instrument_with_bar_sessions(
        ibkr_stock_payload("SPY"),
        raw_bars,
    )
    route = route_instrument(instrument, expected_source="ibkr")
    provenance = canonical_storage_bar_provenance(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    bars = [replace(bar, symbol="SPY", provenance=provenance) for bar in raw_bars]
    micro = [
        replace(bar, symbol="SPY", provenance=provenance)
        for bar in _micro_bars(
            rejection_offsets={8, 12, 17, 22},
            forming_offsets={22},
        )
    ]
    indicator_params = {
        indicator_id: {"enabled": indicator_id in {"impulse_fib", "rounded_reversal"}}
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.pipeline_stage != "ui"
    }
    indicator_params["global_defaults"] = {}
    parent_end = bars[-1].ts + timedelta(minutes=5)

    snapshot = analyze_bars(
        bars,
        instrument=instrument,
        indicator_params=indicator_params,
        mtf_context={"1m": micro},
        analysis_as_of_utc=parent_end,
    )

    rounded = snapshot["indicators"]["rounded_reversal"]["latest"]
    assert rounded["confirmation_pending"] is True
    assert rounded["micro_rejection_cluster"]["reason_code"] == ("micro_provider_finality_pending")
    assert snapshot["indicators"]["impulse_fib"]["status"]["calculates"]


def test_params_are_bounded_and_tick_size_is_explicit() -> None:
    params = build_params(
        {
            "track_mode": "FAST",
            "sensitivity": "STRICT",
            "scan_bars": 10_000,
            "pivot_len": 0,
            "min_rise_bars": 20,
            "max_rise_bars": 2,
            "min_height_atr": -5,
            "min_rejections": 50,
            "micro_min_rejections": 50,
            "micro_min_coverage": 0.1,
            "use_mtf_rejections": "false",
        }
    )
    assert params.track_mode == "fast"
    assert params.sensitivity == "strict"
    assert params.scan_bars == 480
    assert params.pivot_len == 2
    assert params.min_rise_bars == 20
    assert params.max_rise_bars == 20
    assert params.min_height_atr == 0.5
    assert params.min_rejections == 8
    assert params.micro_min_rejections == 12
    assert params.micro_min_coverage == 0.5
    assert params.use_mtf_rejections is False

    with pytest.raises(ValueError, match="TICK_SIZE_INVALID"):
        rounded_reversal(_rounded_top_bars(), tick_size=0.0)
