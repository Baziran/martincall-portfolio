from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta, timezone

import pytest

from aef_terminal.data.gex.contracts import gex_capture_revision_at
from aef_terminal.data.gex.dynamics import gex_dynamics
from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
    bar_revision_signature,
)
from aef_terminal.engine.serialization import serialize_candidate, serialize_decision
from aef_terminal.engine.snapshot.range import _same_bar_series
from aef_terminal.indicators.modules.option_reversal import (
    OptionReversalParams,
    option_reversal as _option_reversal,
)
from aef_terminal.runtime.math_utils import float_or_none


def option_reversal(bars, option_targets=None, params=None, **kwargs):
    return _option_reversal(
        bars,
        option_targets=option_targets,
        params=params,
        **kwargs,
    )


def _bars_from_closes(closes: list[float], *, symbol: str = "ES") -> list[Bar]:
    start = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        bars.append(
            Bar(
                symbol=symbol,
                ts=start + timedelta(minutes=index),
                open=close,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=1000 + index,
                timeframe="1m",
            )
        )
    return bars


def test_canonical_optional_float_rejects_boolean_values() -> None:
    assert float_or_none(True) is None
    assert float_or_none(False) is None
    assert float_or_none("-1.25") == -1.25


def _option_bars(count: int = 40) -> list[Bar]:
    price = 100.0
    bars: list[Bar] = []
    for index in range(count):
        close = price + 0.05
        bars.append(
            Bar(
                "ES",
                datetime(2026, 1, 1, 14, 30, tzinfo=UTC) + timedelta(minutes=index),
                price,
                close + 0.2,
                price - 0.2,
                close,
                1000,
                "1m",
            )
        )
        price = close
    return bars


def test_bar_is_a_canonical_finite_utc_ohlcv_value() -> None:
    source_ts = datetime(2026, 1, 1, 17, 30, tzinfo=timezone(timedelta(hours=3)))
    bar = Bar("CL", source_ts, -2.0, 1.0, -3.0, -1.0, 0, "1m")

    assert bar.ts == datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    assert bar.ts.tzinfo is UTC
    assert bar.open == -2.0
    assert bar.volume == 0.0

    with pytest.raises(ValueError, match="BAR_TIMESTAMP_NOT_AWARE"):
        Bar("CL", datetime(2026, 1, 1, 14, 30), 1, 2, 0, 1, 0)
    with pytest.raises(ValueError, match="BAR_OPEN_NOT_FINITE"):
        Bar("CL", source_ts, float("nan"), 2, 0, 1, 0)
    with pytest.raises(ValueError, match="BAR_VOLUME_NOT_FINITE"):
        Bar("CL", source_ts, 1, 2, 0, 1, float("inf"))
    with pytest.raises(ValueError, match="BAR_VOLUME_NEGATIVE"):
        Bar("CL", source_ts, 1, 2, 0, 1, -1)
    with pytest.raises(ValueError, match="BAR_OHLC_GEOMETRY_INVALID"):
        Bar("CL", source_ts, 1, 0.5, 0, 1, 0)
    with pytest.raises(TypeError, match="BAR_CLOSED_NOT_BOOL"):
        Bar("CL", source_ts, 1, 2, 0, 1, 0, closed=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="BAR_SYMBOL_INVALID"):
        Bar("", source_ts, 1, 2, 0, 1, 0)


def test_mapping_bar_revision_signature_rejects_missing_or_invalid_values() -> None:
    payload = {
        "ts": "2026-01-01T17:30:00+03:00",
        "open": 0.0,
        "high": 1.0,
        "low": -1.0,
        "close": 0.0,
        "volume": 0.0,
        "source": "provider:stream",
        "closed": True,
        "state": "confirmed",
    }

    signature = bar_revision_signature(payload)
    assert signature[0] == datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    assert signature[1:6] == (0.0, 1.0, -1.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="BAR_SIGNATURE_OPEN_MISSING"):
        bar_revision_signature({**payload, "open": None})
    with pytest.raises(ValueError, match="BAR_SIGNATURE_CLOSE_NOT_FINITE"):
        bar_revision_signature({**payload, "close": float("nan")})
    with pytest.raises(TypeError, match="BAR_SIGNATURE_CLOSED_NOT_BOOL"):
        bar_revision_signature({**payload, "closed": 1})


def test_same_bar_series_compares_the_full_bar_revision() -> None:
    original = Bar(
        "CL",
        datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        100,
        102,
        99,
        101,
        10,
        "1m",
        source="provider:history",
    )

    assert _same_bar_series([original], [original])
    assert not _same_bar_series(
        [original],
        [
            Bar(
                "CL",
                original.ts,
                100,
                103,
                99,
                102,
                11,
                "1m",
                source="provider:history",
            )
        ],
    )
    assert not _same_bar_series(
        [original],
        [
            Bar(
                "CL",
                original.ts,
                100,
                102,
                99,
                101,
                10,
                "1m",
                source="provider:live",
            )
        ],
    )


def test_signal_and_decision_values_fail_closed_and_are_deeply_immutable() -> None:
    finality_field = next(field for field in fields(SignalCandidate) if field.name == "finality")
    assert finality_field.kw_only is True
    assert finality_field.default is MISSING

    source_details = {"trade_plan": {"trigger": 100.0}, "evidence": ["confirmed"]}
    candidate = SignalCandidate(
        "setup",
        Direction.LONG,
        84,
        100,
        "setup",
        DomainFact("setup"),
        details=source_details,
        kind=ScenarioKind.TRANSIT,
        producer_phase=ActionPhase.ARM,
        finality=CandidateFinality.CONFIRMED,
    )
    assert candidate.producer_phase is ActionPhase.ARM
    source_details["trade_plan"]["trigger"] = 999.0
    assert candidate.details["trade_plan"]["trigger"] == 100.0
    with pytest.raises(TypeError):
        candidate.details["trade_plan"]["trigger"] = 101.0
    with pytest.raises(ValueError, match="SignalCandidate.score must be a finite number"):
        SignalCandidate(
            "nan",
            Direction.LONG,
            float("nan"),
            100,
            "nan",
            DomainFact("nan"),
            finality=CandidateFinality.CONFIRMED,
        )
    with pytest.raises(ValueError, match="SignalCandidate.level must be a finite number"):
        SignalCandidate(
            "nan",
            Direction.LONG,
            80,
            float("inf"),
            "nan",
            DomainFact("nan"),
            finality=CandidateFinality.CONFIRMED,
        )
    with pytest.raises(TypeError, match="must be a Direction"):
        SignalCandidate(
            "bad",
            "long",  # type: ignore[arg-type]
            80,
            100,
            "bad",
            DomainFact("bad"),
            finality=CandidateFinality.CONFIRMED,
        )
    with pytest.raises(TypeError, match="producer_phase must be an ActionPhase"):
        SignalCandidate(
            "bad_phase",
            Direction.LONG,
            80,
            100,
            "bad phase",
            DomainFact("bad_phase"),
            producer_phase="ARM",  # type: ignore[arg-type]
            finality=CandidateFinality.CONFIRMED,
        )
    with pytest.raises(ValueError, match="must be a candidate entry phase"):
        SignalCandidate(
            "wait_phase",
            Direction.LONG,
            80,
            100,
            "wait phase",
            DomainFact("wait_phase"),
            producer_phase=ActionPhase.WAIT,
            finality=CandidateFinality.CONFIRMED,
        )

    metrics = {"rank": {"score": 84.0}, "facts": ["confirmed"]}
    decision = ScenarioDecision(
        ScenarioKind.TRANSIT,
        Direction.LONG,
        84,
        ActionPhase.GO,
        100,
        98,
        104,
        98,
        DomainFact("setup"),
        ["confirmed"],
        reason_codes=["setup"],
        metrics=metrics,
    )
    metrics["rank"]["score"] = 0.0
    assert decision.metrics["rank"]["score"] == 84.0
    assert decision.reasons == ("confirmed",)
    assert decision.reason_codes == ("setup",)
    with pytest.raises(TypeError):
        decision.metrics["rank"]["score"] = 0.0
    with pytest.raises(AttributeError):
        decision.reasons.append("mutated")  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="ScenarioDecision.confidence must be a finite number"):
        ScenarioDecision(
            ScenarioKind.TRANSIT,
            Direction.LONG,
            float("nan"),
            ActionPhase.GO,
            100,
            98,
            104,
            98,
            DomainFact("nan_confidence"),
        )
    with pytest.raises(ValueError, match="ScenarioDecision.target must be a finite number"):
        ScenarioDecision(
            ScenarioKind.TRANSIT,
            Direction.LONG,
            84,
            ActionPhase.GO,
            100,
            98,
            float("nan"),
            98,
            DomainFact("nan_target"),
        )
    with pytest.raises(TypeError, match="ScenarioDecision.action must be an ActionPhase"):
        ScenarioDecision(
            ScenarioKind.TRANSIT,
            Direction.LONG,
            84,
            "GO",  # type: ignore[arg-type]
            100,
            98,
            104,
            98,
            DomainFact("string_action"),
        )
    with pytest.raises(TypeError, match="ScenarioDecision.trigger must be a finite number"):
        ScenarioDecision(
            ScenarioKind.TRANSIT,
            Direction.LONG,
            84,
            ActionPhase.GO,
            "100",  # type: ignore[arg-type]
            98,
            104,
            98,
            DomainFact("string_trigger"),
        )


def test_gex_migration_codes_are_context_only() -> None:
    long_bars = _bars_from_closes([100 + index * 0.35 for index in range(48)])
    short_bars = _bars_from_closes([100.0] * 21 + [99.0, 98.0, 97.0, 96.0, 95.0, 94.0])

    def snapshot(
        bar: Bar,
        *,
        call_wall: float,
        put_wall: float,
        activity: dict | None = None,
    ) -> dict:
        prices = sorted({92.0, 96.0, 100.0, 108.0, 112.0, call_wall, put_wall})
        exposure_rows: list[tuple[float, float, float]] = []
        for price in prices:
            call_gex = 200.0 if price == call_wall else 100.0
            put_gex = -200.0 if price == put_wall else -20.0
            exposure_rows.append((price, call_gex, put_gex))
        peak_abs_gex = max(
            abs(call_gex) + abs(put_gex) for _price, call_gex, put_gex in exposure_rows
        )
        levels = []
        for rank, (price, call_gex, put_gex) in enumerate(exposure_rows, start=1):
            abs_gex = abs(call_gex) + abs(put_gex)
            strength = round(abs_gex / peak_abs_gex, 4)
            kind = (
                "CALL_WALL"
                if price == call_wall
                else "PUT_WALL"
                if price == put_wall
                else "GEX_NODE"
            )
            levels.append(
                {
                    "price": price,
                    "kind": kind,
                    "kind_class": {
                        "CALL_WALL": "call",
                        "PUT_WALL": "put",
                        "GEX_NODE": "neutral_node",
                    }[kind],
                    "power_class": (
                        "WEAK"
                        if strength < 0.10
                        else "EXTREME"
                        if strength >= 0.85
                        else "STRONG"
                        if strength >= 0.55
                        else "MEDIUM"
                    ),
                    "strength": strength,
                    "selection_rank": rank,
                    "net_gex": call_gex + put_gex,
                    "call_gex": call_gex,
                    "put_gex": put_gex,
                    "abs_gex": abs_gex,
                    "abs_flow_1pt": abs_gex / (float(bar.close) * 0.01),
                    "distance_from_spot": price - float(bar.close),
                    "zone_half_width": 0.25,
                    "spot_side": (
                        "above"
                        if price > float(bar.close) * 1.000001
                        else "below"
                        if price < float(bar.close) * 0.999999
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
        return {
            "captured_at": bar.ts.isoformat(),
            "capture_revision": gex_capture_revision_at(
                bar.ts,
                capture_mode="request",
            ).isoformat(),
            "instrument_id": "ibkr:ES:202606",
            "route_fingerprint": "route-es-202606",
            "provider_symbol": "ES",
            "source": "gex:ibkr",
            "capture_mode": "request",
            "comparison_scope": {
                "capture_mode": "request",
                "strike_count": len(prices),
                "strike_ladder": [float(price) for price in prices],
                "contract_con_ids": list(range(1, len(prices) * 4 + 1)),
                "expiries": ["20260101", "20260102"],
                "futures_options": True,
                "series": [
                    {
                        "expiry": expiry,
                        "trading_class": "ES",
                        "exchange": "CME",
                        "multiplier": 50.0,
                    }
                    for expiry in ("20260101", "20260102")
                ],
                "risk_free_rate": 0.04,
                "dividend_yield": 0.0,
                "market_data_entitlement": "live",
            },
            "frame_complete": True,
            "decision_authoritative": True,
            "market_data_entitlement": "live",
            "open_interest_as_of": "previous_settlement",
            "spot": float(bar.close),
            "levels": levels,
            "call_wall": call_wall,
            "put_wall": put_wall,
            "net_gex": net_gex,
            "gamma_flip": 100.0,
            "global_gamma_regime": ("POSITIVE_ESTIMATE" if net_gex > 0 else "NEGATIVE_ESTIMATE"),
            "option_activity": activity,
            "option_activity_status": (
                activity.get("status") if isinstance(activity, dict) else "NEED_PREVIOUS"
            ),
        }

    activity = {
        "status": "READY",
        "scope": "selected_chain",
        "elapsed_seconds": 2 * 60.0,
        "contract_universe": {
            "selected_strikes": 5,
            "selected_contracts": 10,
            "matched_contracts": 10,
        },
        "call": {
            "volume_delta": 20.0,
            "rate_per_minute": 10.0,
            "rvol": 2.0,
            "baseline_samples": 3,
        },
        "put": {
            "volume_delta": 10.0,
            "rate_per_minute": 5.0,
            "rvol": 1.0,
            "baseline_samples": 3,
        },
        "total": {
            "volume_delta": 30.0,
            "rate_per_minute": 15.0,
            "rvol": 1.5,
            "baseline_samples": 3,
        },
    }
    long_history = [
        snapshot(long_bars[40], call_wall=108.0, put_wall=96.0),
        snapshot(long_bars[42], call_wall=112.0, put_wall=96.0, activity=activity),
    ]
    short_history = [
        snapshot(short_bars[20], call_wall=110.0, put_wall=96.0),
        snapshot(short_bars[22], call_wall=110.0, put_wall=92.0, activity=activity),
    ]

    long_result = gex_dynamics(long_bars, long_history)
    short_result = gex_dynamics(short_bars, short_history)
    long_migration = long_result["series"][42]
    short_migration = short_result["series"][22]

    assert long_migration["state"] == "MIGRATION"
    assert short_migration["state"] == "MIGRATION"
    assert long_migration["call_wall_motion"]["state"] == "UP"
    assert short_migration["put_wall_motion"]["state"] == "DOWN"
    assert long_migration["context_only"] is True
    assert short_migration["context_only"] is True
    assert long_migration["direction"] == "flat"
    assert short_migration["direction"] == "flat"
    assert long_migration["action"] == "WAIT"
    assert short_migration["action"] == "WAIT"
    assert "signal" not in long_migration
    assert "signal" not in short_migration
    assert long_result["events"] == short_result["events"] == []


def test_option_reversal_right_matches_execution_direction_and_bridge_bias() -> None:
    bars = _option_bars()
    latest = bars[-1].close
    sampled_at = datetime.now(tz=UTC)
    previous_at = sampled_at - timedelta(seconds=2)

    call_result = option_reversal(
        bars,
        [
            {
                "id": "call",
                "symbol": "ES",
                "point": {"price": latest},
                "payload": {
                    "intent": {"right": "C", "sec_type": "FOP"},
                    "market_sample": {
                        "fair_price": 10.0,
                        "fair_price_status": "ok",
                        "reference_option_price": 7.9,
                        "reference_option_price_source": "bid_ask_mid",
                        "live_bid": 7.8,
                        "live_ask": 8.0,
                        "live_quote_status": "ok",
                        "live_quote_entitlement": "live",
                        "live_quote_ts": sampled_at.isoformat(),
                        "live_quote_time_basis": "client_receive",
                        "live_underlying_price": latest,
                        "underlying_quote_price": latest,
                        "underlying_quote_price_source": "bid_ask_mid",
                        "underlying_quote_entitlement": "live",
                        "underlying_quote_status": "live",
                        "underlying_quote_ts": sampled_at.isoformat(),
                        "underlying_quote_time_basis": "client_receive",
                    },
                },
            }
        ],
        OptionReversalParams(),
        analysis_as_of_utc=sampled_at,
    )
    put_result = option_reversal(
        bars,
        [
            {
                "id": "put",
                "symbol": "ES",
                "point": {"price": latest},
                "payload": {
                    "intent": {"right": "P", "sec_type": "FOP"},
                    "market_sample": {
                        "fair_price": 8.05,
                        "fair_price_status": "ok",
                        "reference_option_price": 7.975,
                        "reference_option_price_source": "bid_ask_mid",
                        "live_bid": 7.9,
                        "live_ask": 8.05,
                        "live_quote_status": "ok",
                        "live_quote_entitlement": "live",
                        "live_quote_ts": sampled_at.isoformat(),
                        "live_quote_time_basis": "client_receive",
                        "live_underlying_price": latest,
                        "underlying_quote_price": latest,
                        "underlying_quote_price_source": "bid_ask_mid",
                        "underlying_quote_entitlement": "live",
                        "underlying_quote_status": "live",
                        "underlying_quote_ts": sampled_at.isoformat(),
                        "underlying_quote_time_basis": "client_receive",
                        "previous_reference_option_price": 8.0,
                        "previous_underlying_price": latest + 0.25,
                        "previous_live_quote_ts": previous_at.isoformat(),
                        "previous_live_quote_time_basis": "client_receive",
                        "previous_underlying_quote_ts": previous_at.isoformat(),
                        "previous_underlying_quote_time_basis": "client_receive",
                    },
                },
            }
        ],
        OptionReversalParams(),
        analysis_as_of_utc=sampled_at,
    )

    assert call_result["latest"]["action"] == "BUY_CALL"
    assert call_result["latest"]["direction"] == "long"
    assert call_result["latest"]["monte_carlo_bridge"]["bias_settings"]["pine_bias"] >= 0
    assert put_result["latest"]["action"] == "BUY_PUT"
    assert put_result["latest"]["direction"] == "short"
    assert put_result["latest"]["monte_carlo_bridge"]["bias_settings"]["pine_bias"] < 0


def test_serialized_decision_blocks_incoherent_trade_geometry() -> None:
    long_decision = ScenarioDecision(
        ScenarioKind.TRANSIT,
        Direction.LONG,
        80,
        ActionPhase.GO,
        100,
        98,
        104,
        98,
        DomainFact("long_setup"),
        ["long setup"],
    )
    short_decision = ScenarioDecision(
        ScenarioKind.FADE,
        Direction.SHORT,
        80,
        ActionPhase.GO,
        100,
        102,
        96,
        102,
        DomainFact("short_setup"),
        ["short setup"],
    )
    bad_decision = ScenarioDecision(
        ScenarioKind.TRANSIT,
        Direction.LONG,
        80,
        ActionPhase.GO,
        100,
        102,
        96,
        102,
        DomainFact("bad_long_geometry"),
        ["bad long geometry"],
    )

    assert serialize_decision(long_decision)["action"] == "GO"
    assert serialize_decision(short_decision)["action"] == "GO"
    blocked = serialize_decision(bad_decision)
    assert blocked["action"] == "BLOCK"
    assert blocked["coherent"] is False
    assert blocked["raw_plan"] == {"trigger": 100, "stop": 102, "target": 96, "invalidation": 102}
    assert "incoherent_trade_plan_geometry" in blocked["reason_codes"]


def test_serialized_decision_preserves_zero_stop_over_invalidation() -> None:
    decision = ScenarioDecision(
        ScenarioKind.TRANSIT,
        Direction.LONG,
        80,
        ActionPhase.GO,
        1,
        0,
        2,
        2,
        DomainFact("zero_stop_setup"),
        ["zero stop remains valid"],
    )

    payload = serialize_decision(decision)

    assert payload["action"] == "GO"
    assert payload["stop"] == 0


def test_preview_candidates_are_not_decision_eligible() -> None:
    candidate = SignalCandidate(
        "sfp",
        Direction.LONG,
        70,
        100,
        "live sweep preview",
        DomainFact("live_sweep_preview"),
        details={"live_bar": True, "execution_candidate": False},
        finality=CandidateFinality.PROVISIONAL,
    )

    payload = serialize_candidate(candidate)

    assert payload["status"]["stage"] == "preview"
    assert payload["status"]["confirmation"] == "provisional"
    assert payload["status"]["finality"] == "provisional"
    assert payload["status"]["execution_candidate"] is False
    assert payload["status"]["decision_eligible"] is False


def test_candidate_serialization_has_no_default_quarter_tick_fallback() -> None:
    candidate = SignalCandidate(
        "exact_level",
        Direction.LONG,
        70,
        100.1,
        "exact level",
        DomainFact("exact_level"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    assert serialize_candidate(candidate)["level"] == 100.1
    assert serialize_candidate(candidate, price_increment=0.25)["level"] == 100.0
