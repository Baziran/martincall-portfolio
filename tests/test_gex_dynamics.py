from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.gex.contracts import gex_capture_revision_at
from aef_terminal.data.gex.dynamics import GexDynamicsParams, gex_dynamics
from aef_terminal.domain import Bar
from aef_terminal.engine.analyze import analyze_bars
from aef_terminal.engine.gex_context import build_gex_dynamics_context
from aef_terminal.engine.snapshot.empty import empty_market_snapshot
from tests.provider_payloads import ibkr_future_payload


def _bars(closes: list[float] | None = None, count: int = 48) -> list[Bar]:
    values = closes or [100.0 + index * 0.35 for index in range(count)]
    start = datetime(2026, 5, 27, 8, 0, tzinfo=UTC)
    return [
        Bar(
            symbol="ES",
            ts=start + timedelta(minutes=index * 5),
            open=close,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=1000 + index,
            timeframe="5m",
            source="test",
            closed=True,
        )
        for index, close in enumerate(values)
    ]


def _activity(
    *,
    call_volume: float = 100.0,
    call_rvol: float | None = 2.2,
    put_volume: float = 20.0,
    put_rvol: float | None = 1.1,
    status: str = "READY",
) -> dict[str, Any]:
    return {
        "status": status,
        "scope": "selected_chain",
        "elapsed_seconds": 600.0,
        "contract_universe": {
            "selected_strikes": 5,
            "selected_contracts": 10,
            "matched_contracts": 10,
        },
        "call": {
            "volume_delta": call_volume,
            "rate_per_minute": call_volume / 10.0,
            "rvol": call_rvol,
            "baseline_samples": 4 if call_rvol is not None else 0,
        },
        "put": {
            "volume_delta": put_volume,
            "rate_per_minute": put_volume / 10.0,
            "rvol": put_rvol,
            "baseline_samples": 4 if put_rvol is not None else 0,
        },
        "total": {
            "volume_delta": call_volume + put_volume,
            "rate_per_minute": (call_volume + put_volume) / 10.0,
            "rvol": None if status == "BASELINING" else 1.8,
            "baseline_samples": 0 if status == "BASELINING" else 4,
        },
    }


def _snapshot(
    bar: Bar,
    *,
    call_wall: float | None = 108.0,
    put_wall: float | None = 96.0,
    gamma_flip: float | None = 101.0,
    prices: tuple[float, ...] = (92.0, 96.0, 100.0, 108.0, 112.0),
    expiries: tuple[str, ...] = ("20260527", "20260528"),
    futures_options: bool = True,
    capture_mode: str = "request",
    instrument_id: str = "ibkr:ES:202606",
    route_fingerprint: str = "route-es-202606",
    risk_free_rate: float = 0.04,
    dividend_yield: float = 0.0,
    market_data_entitlement: str = "live",
    contract_con_id_offset: int = 0,
    activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spot = float(bar.close)
    exact_prices = tuple(
        sorted(
            {
                *prices,
                *(value for value in (call_wall, put_wall) if value is not None),
            }
        )
    )
    exposure_rows = []
    for price in exact_prices:
        call_gex = (
            200.0
            if call_wall is not None and price == call_wall
            else 100.0
            if call_wall is not None
            else 0.0
        )
        put_gex = (
            -200.0
            if put_wall is not None and price == put_wall
            else -20.0
            if put_wall is not None
            else 0.0
        )
        exposure_rows.append((price, call_gex, put_gex))
    peak_abs_gex = max(
        (abs(call_gex) + abs(put_gex) for _, call_gex, put_gex in exposure_rows),
        default=0.0,
    )
    levels = []
    for rank, (price, call_gex, put_gex) in enumerate(exposure_rows, start=1):
        kind = (
            "CALL_WALL" if price == call_wall else "PUT_WALL" if price == put_wall else "GEX_NODE"
        )
        abs_gex = abs(call_gex) + abs(put_gex)
        strength = round(abs_gex / peak_abs_gex, 4) if peak_abs_gex > 0 else 0.0
        power_class = (
            "WEAK"
            if strength < 0.10
            else "EXTREME"
            if strength >= 0.85
            else "STRONG"
            if strength >= 0.55
            else "MEDIUM"
        )
        levels.append(
            {
                "price": float(price),
                "kind": kind,
                "kind_class": {
                    "CALL_WALL": "call",
                    "PUT_WALL": "put",
                    "GEX_NODE": "neutral_node",
                }[kind],
                "strength": strength,
                "power_class": power_class,
                "selection_rank": rank,
                "net_gex": call_gex + put_gex,
                "call_gex": call_gex,
                "put_gex": put_gex,
                "abs_gex": abs_gex,
                "abs_flow_1pt": abs_gex / (spot * 0.01),
                "distance_from_spot": float(price) - spot,
                "zone_half_width": 0.25,
                "spot_side": (
                    "above"
                    if price > spot * 1.000001
                    else "below"
                    if price < spot * 0.999999
                    else "inside"
                ),
                "option_volume_context": {
                    "current": {
                        "call_volume": None,
                        "put_volume": None,
                        "total_volume": None,
                        "call_oi": None,
                        "put_oi": None,
                        "total_oi": None,
                        "turnover": None,
                        "rank": None,
                    },
                    "event": None,
                },
            }
        )
    net_gex = sum(float(level["net_gex"]) for level in levels)
    series = [
        {
            "expiry": expiry,
            "trading_class": "ES",
            "exchange": "CME",
            "multiplier": 50.0,
        }
        for expiry in expiries
    ]
    return {
        "captured_at": bar.ts.isoformat(),
        "capture_revision": gex_capture_revision_at(
            bar.ts,
            capture_mode=capture_mode,
        ).isoformat(),
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider_symbol": "ES",
        "source": "gex:ibkr-live" if capture_mode == "live" else "gex:ibkr",
        "capture_mode": capture_mode,
        "frame_complete": True,
        "decision_authoritative": market_data_entitlement == "live",
        "market_data_entitlement": market_data_entitlement,
        "open_interest_as_of": "previous_settlement",
        "comparison_scope": {
            "capture_mode": capture_mode,
            "strike_count": len(exact_prices),
            "strike_ladder": [float(price) for price in exact_prices],
            "contract_con_ids": list(
                range(
                    contract_con_id_offset + 1,
                    contract_con_id_offset + len(exact_prices) * len(expiries) * 2 + 1,
                )
            ),
            "expiries": list(expiries),
            "futures_options": futures_options,
            "series": series,
            "risk_free_rate": risk_free_rate,
            "dividend_yield": dividend_yield,
            "market_data_entitlement": market_data_entitlement,
        },
        "spot": spot,
        "levels": levels,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "net_gex": net_gex,
        "gamma_flip": gamma_flip,
        "global_gamma_regime": (
            "UNKNOWN"
            if net_gex == 0
            else "POSITIVE_ESTIMATE"
            if net_gex > 0
            else "NEGATIVE_ESTIMATE"
        ),
        "option_activity": activity,
        "option_activity_status": (
            activity.get("status") if isinstance(activity, dict) else "NEED_PREVIOUS"
        ),
    }


def _history(
    bars: list[Bar],
    *,
    call_wall: float = 112.0,
    put_wall: float = 96.0,
    gamma_flip: float = 101.0,
) -> list[dict[str, Any]]:
    return [
        _snapshot(bars[-4], call_wall=108.0, put_wall=96.0, gamma_flip=101.0),
        _snapshot(
            bars[-2],
            call_wall=call_wall,
            put_wall=put_wall,
            gamma_flip=gamma_flip,
            activity=_activity(),
        ),
    ]


def test_gex_dynamics_emits_migration_once_as_context_without_trade_signal() -> None:
    bars = _bars()
    result = gex_dynamics(bars, _history(bars))
    migration = result["series"][-2]

    assert migration["state"] == "MIGRATION"
    assert migration["comparison_quality"] == "READY"
    assert migration["call_wall_motion"]["state"] == "UP"
    assert migration["put_wall_motion"]["state"] == "UNCHANGED"
    assert migration["gamma_flip_motion"]["state"] == "UNCHANGED"
    assert migration["wall_span_motion"]["state"] == "UP"
    assert migration["structure_state"] == "CALL_WALL_MOVING"
    assert migration["context_only"] is True
    assert migration["action"] == "WAIT"
    assert migration["direction"] == "flat"
    assert migration["score"] == 0.0
    assert "signal" not in migration
    assert migration["evidence"]["context"] == [
        {"code": "context_only"},
        {"code": "not_market_phase"},
    ]
    assert result["version"] == "2.2-python"
    assert result["latest"]["state"] == "NO_KEY_LEVEL_MIGRATION"
    assert result["latest"]["structure_state"] == "CALL_WALL_MOVING"
    assert sum(row["state"] == "MIGRATION" for row in result["series"]) == 1
    assert result["events"] == []


def test_gex_dynamics_detects_put_and_gamma_flip_motion_independently() -> None:
    bars = _bars([100.0] * 27, count=27)
    history = [
        _snapshot(bars[20], call_wall=110.0, put_wall=96.0, gamma_flip=99.0),
        _snapshot(
            bars[22],
            call_wall=110.0,
            put_wall=92.0,
            gamma_flip=95.0,
            activity=_activity(),
        ),
    ]

    result = gex_dynamics(bars, history)
    migration = result["series"][22]

    assert migration["state"] == "MIGRATION"
    assert migration["call_wall_motion"]["state"] == "UNCHANGED"
    assert migration["put_wall_motion"]["state"] == "DOWN"
    assert migration["gamma_flip_motion"]["state"] == "DOWN"
    assert migration["structure_state"] == "SHIFT_LOWER"
    assert migration["direction"] == "flat"
    assert "signal" not in migration
    assert result["series"][23]["state"] == "NO_KEY_LEVEL_MIGRATION"
    assert result["events"] == []


def test_gex_dynamics_preserves_both_proximities_and_side_specific_chain_activity() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[26], call_wall=100.0, put_wall=100.0, gamma_flip=100.0),
        _snapshot(
            bars[28],
            call_wall=100.0,
            put_wall=100.0,
            gamma_flip=100.0,
            activity=_activity(call_volume=100.0, call_rvol=2.2, put_volume=0.0),
        ),
    ]

    latest = gex_dynamics(bars, history)["latest"]

    assert latest["state"] == "NEAR"
    assert [item["side"] for item in latest["proximities"]] == ["CALL", "PUT"]
    assert {item["price"] for item in latest["proximities"]} == {100.0}
    assert latest["activity_state"]["scope"] == "selected_chain"
    assert latest["activity_state"]["contract_universe"] == {
        "selected_strikes": 5,
        "selected_contracts": 10,
        "matched_contracts": 10,
    }
    assert latest["activity_state"]["call"]["state"] == "ACTIVE"
    assert latest["activity_state"]["put"]["state"] == "QUIET"
    assert latest["structure_state"] == "LEVELS_HELD"
    assert [item["activity_state"] for item in latest["proximities"]] == [
        "ACTIVE",
        "QUIET",
    ]


def test_gex_dynamics_classifies_persistent_level_structure_from_typed_motion() -> None:
    bars = _bars([100.0] * 30, count=30)
    exact_prices = (88.0, 92.0, 96.0, 100.0, 104.0, 108.0, 112.0, 116.0)
    cases = (
        ((108.0, 96.0, 100.0), (108.0, 96.0, 100.0), "LEVELS_HELD", "UNCHANGED"),
        ((108.0, 96.0, 100.0), (112.0, 100.0, 104.0), "SHIFT_HIGHER", "UNCHANGED"),
        ((112.0, 100.0, 104.0), (108.0, 96.0, 100.0), "SHIFT_LOWER", "UNCHANGED"),
        ((108.0, 96.0, 100.0), (112.0, 92.0, 100.0), "WALLS_WIDENING", "UP"),
        ((112.0, 92.0, 100.0), (108.0, 96.0, 100.0), "WALLS_NARROWING", "DOWN"),
        ((108.0, 96.0, 100.0), (112.0, 96.0, 100.0), "CALL_WALL_MOVING", "UP"),
        ((108.0, 96.0, 100.0), (108.0, 92.0, 100.0), "PUT_WALL_MOVING", "UP"),
        ((108.0, 96.0, 100.0), (108.0, 96.0, 104.0), "GAMMA_FLIP_MOVING", "UNCHANGED"),
        ((108.0, 96.0, 100.0), (112.0, 96.0, 96.0), "MIXED_MIGRATION", "UP"),
    )

    for previous_values, current_values, expected_state, expected_span_state in cases:
        previous_call, previous_put, previous_flip = previous_values
        current_call, current_put, current_flip = current_values
        history = [
            _snapshot(
                bars[26],
                call_wall=previous_call,
                put_wall=previous_put,
                gamma_flip=previous_flip,
                prices=exact_prices,
            ),
            _snapshot(
                bars[28],
                call_wall=current_call,
                put_wall=current_put,
                gamma_flip=current_flip,
                prices=exact_prices,
                activity=_activity(),
            ),
        ]

        result = gex_dynamics(bars, history)
        event_row = result["series"][28]
        latest = result["latest"]

        assert event_row["comparison_quality"] == "READY"
        assert event_row["structure_state"] == expected_state
        assert event_row["wall_span_motion"]["state"] == expected_span_state
        assert latest["structure_state"] == expected_state
        assert latest["action"] == "WAIT"
        assert latest["direction"] == "flat"
        assert latest["score"] == 0.0
        assert "signal" not in latest


def test_gex_dynamics_classifies_wall_motion_when_gamma_flip_is_unavailable() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(
            bars[26],
            call_wall=108.0,
            put_wall=96.0,
            gamma_flip=None,
        ),
        _snapshot(
            bars[28],
            call_wall=108.0,
            put_wall=100.0,
            gamma_flip=None,
            activity=_activity(),
        ),
    ]

    latest = gex_dynamics(bars, history)["latest"]

    assert latest["comparison_quality"] == "READY"
    assert latest["call_wall_motion"]["state"] == "UNCHANGED"
    assert latest["put_wall_motion"]["state"] == "UP"
    assert latest["gamma_flip_motion"]["state"] == "UNKNOWN"
    assert latest["structure_state"] == "PUT_WALL_MOVING"
    assert latest["metrics"]["structure_state"] == "PUT_WALL_MOVING"
    assert latest["state"] == "NEAR"
    assert latest["watch_state"] == "WATCH"


def test_gex_dynamics_keeps_prebaseline_activity_unscaled() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[26], call_wall=100.0, put_wall=100.0),
        _snapshot(
            bars[28],
            call_wall=100.0,
            put_wall=100.0,
            activity=_activity(
                call_rvol=None,
                put_rvol=None,
                status="BASELINING",
            ),
        ),
    ]

    latest = gex_dynamics(bars, history)["latest"]

    assert latest["comparison_quality"] == "READY"
    assert latest["activity_state"]["call"]["state"] == "BASELINING"
    assert latest["activity_state"]["put"]["state"] == "BASELINING"


def test_gex_dynamics_returns_wait_row_with_zero_or_one_snapshot() -> None:
    bars = _bars(count=20)

    empty_latest = gex_dynamics(bars, [])["latest"]
    one_latest = gex_dynamics(bars, [_snapshot(bars[-2])])["latest"]

    assert empty_latest["state"] == "WAIT"
    assert empty_latest["comparison_quality"] == "NEED_HISTORY"
    assert empty_latest["context_only"] is True
    assert empty_latest["action"] == "WAIT"
    assert "signal" not in empty_latest
    assert one_latest["state"] == "WAIT"
    assert one_latest["comparison_quality"] == "NEED_HISTORY"
    assert one_latest["context_only"] is True
    assert one_latest["action"] == "WAIT"
    assert "signal" not in one_latest
    assert one_latest["captured_at"] == bars[-2].ts.isoformat()


def test_gex_dynamics_rejects_snapshot_pair_gap() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[20]),
        _snapshot(bars[24], call_wall=112.0, activity=_activity()),
    ]

    row = gex_dynamics(bars, history)["series"][24]

    assert row["state"] == "WAIT"
    assert row["comparison_quality"] == "GAP"
    assert row["comparison_gap_minutes"] == 20.0
    assert row["call_wall_motion"]["state"] == "UNKNOWN"
    assert row["call_wall_motion"]["delta"] is None


def test_gex_dynamics_separates_every_exact_contract_scope_change() -> None:
    bars = _bars([100.0] * 30, count=30)
    expiry_changed = [
        _snapshot(bars[24], expiries=("20260527",)),
        _snapshot(
            bars[26],
            call_wall=112.0,
            expiries=("20260528",),
            activity=_activity(),
        ),
    ]
    coverage_changed = [
        _snapshot(
            bars[24],
            call_wall=110.0,
            put_wall=90.0,
            prices=(90.0, 95.0, 100.0, 105.0, 110.0),
        ),
        _snapshot(
            bars[26],
            call_wall=120.0,
            put_wall=100.0,
            prices=(100.0, 105.0, 110.0, 115.0, 120.0),
            activity=_activity(),
        ),
    ]
    strike_count_changed = [
        _snapshot(bars[24], prices=(92.0, 96.0, 100.0, 108.0, 112.0)),
        _snapshot(
            bars[26],
            prices=(92.0, 96.0, 100.0, 108.0),
            activity=_activity(),
        ),
    ]
    valuation_changed = [
        _snapshot(bars[24], risk_free_rate=0.04),
        _snapshot(bars[26], risk_free_rate=0.05, activity=_activity()),
    ]
    entitlement_changed = [
        _snapshot(bars[24], market_data_entitlement="live"),
        _snapshot(
            bars[26],
            market_data_entitlement="delayed",
            activity=_activity(),
        ),
    ]

    expiry_row = gex_dynamics(bars, expiry_changed)["series"][26]
    coverage_row = gex_dynamics(bars, coverage_changed)["series"][26]
    strike_count_row = gex_dynamics(bars, strike_count_changed)["series"][26]
    valuation_row = gex_dynamics(bars, valuation_changed)["series"][26]
    entitlement_row = gex_dynamics(bars, entitlement_changed)["series"][26]

    assert expiry_row["comparison_quality"] == "SCOPE_CHANGED"
    assert expiry_row["comparison_issue"] == "comparison_scope_changed"
    assert coverage_row["comparison_quality"] == "SCOPE_CHANGED"
    assert coverage_row["comparison_issue"] == "comparison_scope_changed"
    assert coverage_row["level_price_coverage"] is None
    assert strike_count_row["comparison_quality"] == "SCOPE_CHANGED"
    assert strike_count_row["comparison_issue"] == "comparison_scope_changed"
    assert valuation_row["comparison_quality"] == "SCOPE_CHANGED"
    assert valuation_row["comparison_issue"] == "comparison_scope_changed"
    assert entitlement_row["comparison_quality"] == "SCOPE_CHANGED"
    assert entitlement_row["comparison_issue"] == "comparison_scope_changed"


def test_gex_dynamics_does_not_compare_same_ladder_after_provider_contract_roll() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[24], call_wall=108.0, contract_con_id_offset=0),
        _snapshot(
            bars[26],
            call_wall=112.0,
            contract_con_id_offset=10_000,
            activity=_activity(),
        ),
    ]

    row = gex_dynamics(bars, history)["series"][26]

    assert row["state"] == "WAIT"
    assert row["comparison_quality"] == "SCOPE_CHANGED"
    assert row["comparison_issue"] == "comparison_scope_changed"
    assert row["previous_captured_at"] == bars[24].ts.isoformat()


def test_gex_dynamics_requires_history_for_new_identity_or_capture_lane() -> None:
    bars = _bars([100.0] * 30, count=30)
    identity_changed = [
        _snapshot(bars[24]),
        _snapshot(
            bars[26],
            instrument_id="ibkr:ES:202609",
            route_fingerprint="route-es-202609",
            activity=_activity(),
        ),
    ]
    mode_changed = [
        _snapshot(bars[24], capture_mode="request"),
        _snapshot(bars[26], capture_mode="live", activity=_activity()),
    ]

    identity_row = gex_dynamics(bars, identity_changed)["series"][26]
    mode_row = gex_dynamics(bars, mode_changed)["series"][26]
    live_mode_row = gex_dynamics(
        bars,
        mode_changed,
        params=GexDynamicsParams(capture_mode="live"),
    )["series"][26]

    assert identity_row["comparison_quality"] == "SCOPE_CHANGED"
    assert identity_row["comparison_issue"] == "comparison_scope_changed"
    assert mode_row["comparison_quality"] == "NEED_HISTORY"
    assert mode_row["comparison_issue"] == "need_previous_snapshot"
    assert mode_row["capture_mode"] == "request"
    assert live_mode_row["comparison_quality"] == "NEED_HISTORY"
    assert live_mode_row["capture_mode"] == "live"


def test_gex_dynamics_compares_interleaved_snapshots_within_capture_lane() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[24], capture_mode="request", call_wall=108.0),
        _snapshot(bars[25], capture_mode="live", call_wall=109.0),
        _snapshot(
            bars[26],
            capture_mode="request",
            call_wall=112.0,
            activity=_activity(),
        ),
    ]

    result = gex_dynamics(bars, history)
    first_live = result["series"][25]
    request_migration = result["series"][26]

    assert first_live["comparison_quality"] == "NEED_HISTORY"
    assert request_migration["comparison_quality"] == "READY"
    assert request_migration["state"] == "MIGRATION"
    assert request_migration["capture_mode"] == "request"
    assert request_migration["previous_captured_at"] == bars[24].ts.isoformat()
    assert request_migration["comparison_gap_minutes"] == 10.0
    assert request_migration["call_wall_motion"]["state"] == "UP"
    assert result["series"][27]["state"] == "NO_KEY_LEVEL_MIGRATION"
    assert sum(row["state"] == "MIGRATION" for row in result["series"]) == 1


def test_gex_dynamics_selects_one_exact_capture_lane_and_revision() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[20], capture_mode="request", call_wall=108.0),
        _snapshot(bars[21], capture_mode="live", call_wall=109.0),
        _snapshot(bars[22], capture_mode="request", call_wall=112.0),
        _snapshot(bars[23], capture_mode="live", call_wall=113.0),
    ]

    request_latest = gex_dynamics(bars, history)["latest"]
    live_latest = gex_dynamics(
        bars,
        history,
        params=GexDynamicsParams(capture_mode="live"),
    )["latest"]

    assert request_latest["source"] == "gex:ibkr"
    assert request_latest["capture_mode"] == "request"
    assert request_latest["capture_revision"] == bars[22].ts.isoformat()
    assert request_latest["captured_at"] == bars[22].ts.isoformat()
    assert request_latest["instrument_id"] == "ibkr:ES:202606"
    assert request_latest["route_fingerprint"] == "route-es-202606"
    assert live_latest["source"] == "gex:ibkr-live"
    assert live_latest["capture_mode"] == "live"
    assert live_latest["capture_revision"] == bars[23].ts.isoformat()
    assert live_latest["captured_at"] == bars[23].ts.isoformat()


def test_gex_dynamics_keeps_prior_series_bar_safe_but_publishes_current_snapshot() -> None:
    bars = _bars([100.0] * 30, count=30)
    future_bar = _bars([100.0] * 31, count=31)[-1]
    history = [
        _snapshot(bars[24], call_wall=108.0),
        _snapshot(bars[26], call_wall=112.0, activity=_activity()),
    ]

    baseline = gex_dynamics(bars, history)
    with_future = gex_dynamics(
        bars,
        [
            *history,
            _snapshot(future_bar, call_wall=116.0, activity=_activity()),
        ],
    )

    assert with_future["series"] == baseline["series"]
    assert with_future["latest"] != baseline["latest"]
    assert with_future["latest"]["captured_at"] == future_bar.ts.isoformat()
    assert with_future["latest"]["capture_revision"] == future_bar.ts.isoformat()
    assert with_future["latest"]["ts"] == future_bar.ts.isoformat()
    assert with_future["latest"]["snapshot_effective_now"] is True


def test_gex_dynamics_anchors_motion_tolerance_to_the_snapshot_pair() -> None:
    closes = [100.0] * 23 + [150.0 if index % 2 else 100.0 for index in range(12)]
    bars = _bars(closes, count=len(closes))
    prices = (192.0, 196.0, 200.0, 208.0, 212.0)
    history = [
        _snapshot(
            bars[20],
            call_wall=208.0,
            put_wall=196.0,
            gamma_flip=201.0,
            prices=prices,
        ),
        _snapshot(
            bars[22],
            call_wall=212.0,
            put_wall=196.0,
            gamma_flip=201.0,
            prices=prices,
            activity=_activity(),
        ),
    ]

    result = gex_dynamics(bars, history)
    at_capture = result["series"][22]
    latest = result["latest"]

    assert at_capture["state"] == "MIGRATION"
    assert latest["comparison_quality"] == "READY"
    assert latest["state"] == "NO_KEY_LEVEL_MIGRATION"
    assert latest["call_wall_motion"]["tolerance"] == (at_capture["call_wall_motion"]["tolerance"])


def test_gex_dynamics_resets_on_immediate_invalid_predecessor() -> None:
    bars = _bars([100.0] * 30, count=30)
    invalid = _snapshot(bars[25], call_wall=109.0)
    invalid.pop("comparison_scope")
    history = [
        _snapshot(bars[24], call_wall=108.0),
        invalid,
        _snapshot(
            bars[26],
            call_wall=112.0,
            activity=_activity(),
        ),
    ]

    result = gex_dynamics(bars, history)

    assert result["series"][25]["comparison_quality"] == "INVALID"
    assert result["series"][26]["comparison_quality"] == "INVALID"
    assert result["series"][26]["comparison_issue"] == "invalid_snapshot_payload"
    assert result["series"][26]["previous_captured_at"] == bars[25].ts.isoformat()


def test_gex_dynamics_resets_scope_a_b_a_on_immediate_scope_b() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[24], call_wall=108.0),
        _snapshot(
            bars[25],
            call_wall=109.0,
            futures_options=False,
        ),
        _snapshot(bars[26], call_wall=112.0, activity=_activity()),
    ]

    result = gex_dynamics(bars, history)
    first_scope_b = result["series"][25]
    scope_a_reset = result["series"][26]

    assert first_scope_b["comparison_quality"] == "SCOPE_CHANGED"
    assert scope_a_reset["comparison_quality"] == "SCOPE_CHANGED"
    assert scope_a_reset["state"] == "WAIT"
    assert scope_a_reset["previous_captured_at"] == bars[25].ts.isoformat()
    assert scope_a_reset["comparison_gap_minutes"] == 5.0
    assert scope_a_reset["call_wall_motion"]["state"] == "UNKNOWN"
    assert result["series"][27]["state"] == "WAIT"


def test_gex_dynamics_rejects_mismatched_source_and_scope_capture_lanes() -> None:
    bars = _bars([100.0] * 30, count=30)
    wrong_source = _snapshot(bars[24])
    wrong_source["source"] = "gex:ibkr-live"
    wrong_scope = _snapshot(bars[26])
    wrong_scope["comparison_scope"] = {
        **wrong_scope["comparison_scope"],
        "capture_mode": "live",
    }

    result = gex_dynamics(bars, [wrong_source, wrong_scope])

    assert result["series"][24]["comparison_quality"] == "INVALID"
    assert result["series"][24]["comparison_issue"] == "invalid_capture_mode"
    assert result["series"][26]["comparison_quality"] == "INVALID"
    assert result["series"][26]["comparison_issue"] == "invalid_snapshot_payload"


def test_gex_dynamics_rejects_unsorted_or_duplicate_expiry_scope() -> None:
    bars = _bars([100.0] * 30, count=30)
    unsorted = _snapshot(
        bars[24],
        expiries=("20260528", "20260527"),
    )
    duplicate = _snapshot(
        bars[26],
        expiries=("20260527", "20260527"),
    )

    result = gex_dynamics(bars, [unsorted, duplicate])

    assert result["series"][24]["comparison_issue"] == "invalid_snapshot_payload"
    assert result["series"][26]["comparison_issue"] == "invalid_snapshot_payload"


def test_gex_dynamics_rejects_incomplete_or_inconsistent_option_activity() -> None:
    bars = _bars([100.0] * 30, count=30)
    incomplete_universe = _activity()
    incomplete_universe["contract_universe"] = {
        "selected_strikes": 5,
        "selected_contracts": 10,
        "matched_contracts": 8,
    }
    inconsistent_total = _activity()
    inconsistent_total["total"] = {
        **inconsistent_total["total"],
        "volume_delta": 999.0,
    }

    incomplete_row = gex_dynamics(
        bars,
        [_snapshot(bars[24]), _snapshot(bars[26], activity=incomplete_universe)],
    )["series"][26]
    inconsistent_row = gex_dynamics(
        bars,
        [_snapshot(bars[24]), _snapshot(bars[26], activity=inconsistent_total)],
    )["series"][26]

    assert incomplete_row["comparison_quality"] == "INVALID"
    assert incomplete_row["comparison_issue"] == "invalid_snapshot_payload"
    assert inconsistent_row["comparison_quality"] == "INVALID"
    assert inconsistent_row["comparison_issue"] == "invalid_snapshot_payload"


def test_gex_dynamics_marks_snapshot_stale_after_sixty_minutes() -> None:
    bars = _bars([100.0] * 40, count=40)
    history = [
        _snapshot(bars[20]),
        _snapshot(bars[22], activity=_activity()),
    ]

    result = gex_dynamics(bars, history)
    lone_snapshot = gex_dynamics(bars, [_snapshot(bars[20])])

    assert result["series"][34]["comparison_quality"] == "READY"
    assert result["series"][35]["comparison_quality"] == "STALE"
    assert result["series"][35]["state"] == "WAIT"
    assert lone_snapshot["series"][35]["comparison_quality"] == "STALE"


def test_gex_dynamics_rejects_top_level_walls_that_disagree_with_levels() -> None:
    bars = _bars([100.0] * 30, count=30)
    history = [
        _snapshot(bars[26], call_wall=None, put_wall=None, gamma_flip=None),
        _snapshot(
            bars[28],
            call_wall=None,
            put_wall=None,
            gamma_flip=None,
            activity=_activity(),
        ),
    ]

    latest = gex_dynamics(bars, history)["latest"]

    assert latest["comparison_quality"] == "INVALID"
    assert latest["comparison_issue"] == "invalid_snapshot_payload"
    assert latest["state"] == "WAIT"
    assert latest["watch_state"] == "WAIT"
    assert latest["detail_code"] == "invalid"
    assert latest["call_wall_motion"]["delta"] is None


def test_gex_dynamics_rejects_timestamp_aliases_and_level_derived_walls() -> None:
    bars = _bars(count=20)
    legacy_snapshot = {
        "timestamp": bars[-2].ts.isoformat(),
        "levels": [
            {"kind": "CALL_WALL", "price": 108.0},
            {"kind": "PUT_WALL", "price": 96.0},
        ],
    }

    latest = gex_dynamics(bars, [legacy_snapshot])["latest"]

    assert latest["comparison_quality"] == "INVALID"
    assert latest["comparison_issue"] == "invalid_captured_at"
    assert latest["call_wall"] is None
    assert latest["put_wall"] is None


def test_engine_gex_context_is_compact_and_keeps_dynamics_out_of_candidates() -> None:
    bars = _bars()
    snapshot = analyze_bars(
        bars,
        display_symbol="ES",
        gex_history=_history(bars),
        instrument=ibkr_future_payload("ES"),
        gex_context_active=True,
        gex_capture_mode="request",
    )

    context = snapshot["gex_dynamics"]
    assert context["contract"] == "gex-dynamics-v1"
    assert context["version"] == "3.0-engine"
    assert set(context) == {"contract", "version", "latest", "status"}
    assert "series" not in context
    assert "events" not in context
    assert context["status"]["state"] == "ready"
    assert context["status"]["capture_mode"] == "request"
    assert context["latest"]["context_only"] is True
    assert context["latest"]["action"] == "WAIT"
    assert "signal" not in context["latest"]
    assert "gex_dynamics" not in snapshot["indicators"]
    assert all(candidate.get("source") != "gex_dynamics" for candidate in snapshot["candidates"])


def test_market_snapshot_ignores_legacy_gex_indicator_params_when_context_is_inactive() -> None:
    bars = _bars()
    snapshot = analyze_bars(
        bars,
        display_symbol="ES",
        indicator_params={"gex_dynamics": {"enabled": True, "capture_mode": "live"}},
        gex_history=[
            _snapshot(bars[-4], capture_mode="live"),
            _snapshot(bars[-2], capture_mode="live", activity=_activity()),
        ],
        instrument=ibkr_future_payload("ES"),
        gex_context_active=False,
    )

    assert "gex_dynamics" not in snapshot
    assert "gex_dynamics" not in snapshot["indicators"]


def test_engine_gex_context_selects_the_explicit_live_capture_lane() -> None:
    bars = _bars()
    context = build_gex_dynamics_context(
        bars,
        [
            _snapshot(bars[-4], capture_mode="request"),
            _snapshot(bars[-3], capture_mode="live"),
            _snapshot(bars[-2], capture_mode="request", activity=_activity()),
            _snapshot(bars[-1], capture_mode="live", activity=_activity()),
        ],
        capture_mode="live",
    )

    assert context["status"]["state"] == "ready"
    assert context["status"]["capture_mode"] == "live"
    assert context["latest"]["capture_mode"] == "live"
    assert context["latest"]["source"] == "gex:ibkr-live"
    assert context["latest"]["captured_at"] == bars[-1].ts.isoformat()


def test_engine_gex_context_blocks_empty_and_no_confirmed_snapshots() -> None:
    empty = empty_market_snapshot(
        ibkr_future_payload("ES"),
        "5m",
        "No history",
        gex_context_active=True,
        gex_capture_mode="live",
    )
    provisional = Bar(
        symbol="ES",
        ts=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000,
        timeframe="5m",
        source="test",
        closed=False,
    )
    no_confirmed = analyze_bars(
        [provisional],
        display_symbol="ES",
        instrument=ibkr_future_payload("ES"),
        gex_context_active=True,
        gex_capture_mode="request",
    )

    for snapshot, capture_mode in ((empty, "live"), (no_confirmed, "request")):
        context = snapshot["gex_dynamics"]
        assert context["contract"] == "gex-dynamics-v1"
        assert context["version"] == "3.0-engine"
        assert context["latest"] is None
        assert context["status"]["state"] == "blocked"
        assert context["status"]["reason_code"] == "no_confirmed_bars"
        assert context["status"]["capture_mode"] == capture_mode
        assert context["status"]["bar_count"] == 0
        assert "series" not in context
        assert "events" not in context
