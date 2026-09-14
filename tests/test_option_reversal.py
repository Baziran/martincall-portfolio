from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.gex.option_target_contract import (
    OPTION_TARGET_MARKET_SAMPLE_FIELDS,
)
from aef_terminal.domain import Bar
from aef_terminal.engine.analyze import attach_indicator_status
from aef_terminal.indicators.contracts import validate_indicator_result_contract
from aef_terminal.indicators.modules.option_reversal import (
    OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS,
    OptionReversalParams,
    option_reversal as _option_reversal,
)
from aef_terminal.indicators.registry import indicator_manifest


OPTION_REVERSAL_TEST_AS_OF = datetime(2026, 1, 1, 0, 40, tzinfo=UTC)


def _run_option_reversal(bars, option_targets=None, params=None, **kwargs):
    return _option_reversal(
        bars,
        option_targets=[_target_fixture(item) for item in option_targets or []],
        params=params,
        **kwargs,
    )


def _target_fixture(item: dict) -> dict:
    if isinstance(item.get("payload"), dict):
        return item
    point = item.get("point") if isinstance(item.get("point"), dict) else {}
    point = dict(point)
    point.setdefault("ts", OPTION_REVERSAL_TEST_AS_OF.isoformat())
    intent = {
        "provider_symbol": "ES",
        "mode": "normal",
        "right": item.get("right", "P"),
        "target_dte": "0dte",
        "sec_type": item.get("sec_type", "FOP"),
        "con_id": item.get("con_id", 12345),
        "local_symbol": item.get("local_symbol", "ES P5000"),
        "exchange": "CME",
        "expiry": "20260722",
        "strike": item.get("strike", 5000.0),
    }
    market_sample = {key: item[key] for key in OPTION_TARGET_MARKET_SAMPLE_FIELDS if key in item}
    bid = market_sample.get("live_bid")
    ask = market_sample.get("live_ask")
    last = market_sample.get("live_last")
    if any(value is not None for value in (bid, ask, last)):
        if "live_underlying_price" not in market_sample:
            raise AssertionError("live Option Reversal fixture requires live_underlying_price")
        sample_now = OPTION_REVERSAL_TEST_AS_OF
        original_reference = market_sample.get("reference_option_price")
        if (
            market_sample.get("fair_price") is None
            and isinstance(original_reference, (int, float))
            and not isinstance(original_reference, bool)
            and original_reference > 0
        ):
            market_sample["fair_price"] = original_reference
        if (
            isinstance(bid, (int, float))
            and bid > 0
            and isinstance(ask, (int, float))
            and ask >= bid
        ):
            market_sample["reference_option_price"] = (bid + ask) / 2.0
            market_sample["reference_option_price_source"] = "bid_ask_mid"
        elif isinstance(last, (int, float)) and last > 0:
            market_sample["reference_option_price"] = last
            market_sample["reference_option_price_source"] = "last"
        elif isinstance(ask, (int, float)) and ask > 0:
            market_sample["reference_option_price"] = ask
            market_sample["reference_option_price_source"] = "ask"
        market_sample["live_quote_status"] = "ok"
        market_sample["live_quote_ts"] = sample_now.isoformat()
        market_sample["live_quote_time_basis"] = (
            "provider_event"
            if market_sample.get("reference_option_price_source") == "last"
            else "client_receive"
        )
        market_sample["live_quote_entitlement"] = "live"
        market_sample.setdefault("underlying_quote_price", market_sample["live_underlying_price"])
        market_sample["underlying_quote_price_source"] = "bid_ask_mid"
        market_sample["underlying_quote_entitlement"] = "live"
        market_sample["underlying_quote_status"] = "live"
        market_sample["underlying_quote_ts"] = sample_now.isoformat()
        market_sample["underlying_quote_time_basis"] = "client_receive"
        if market_sample.get("fair_price") is not None:
            market_sample["fair_price_status"] = "ok"
        if (
            market_sample.get("previous_reference_option_price") is not None
            and market_sample.get("previous_underlying_price") is not None
        ):
            previous_at = sample_now - timedelta(seconds=2)
            market_sample.setdefault(
                "previous_live_quote_ts",
                previous_at.isoformat(),
            )
            market_sample.setdefault(
                "previous_live_quote_time_basis",
                "client_receive",
            )
            market_sample.setdefault(
                "previous_underlying_quote_ts",
                previous_at.isoformat(),
            )
            market_sample.setdefault(
                "previous_underlying_quote_time_basis",
                "client_receive",
            )
    canonical = {key: item[key] for key in ("id", "symbol", "timeframe") if key in item}
    canonical["point"] = point
    return canonical | {"payload": {"intent": intent, "market_sample": market_sample}}


def _bars(count: int = 40) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    price = 100.0
    out: list[Bar] = []
    for index in range(count):
        close = price + 0.05
        out.append(
            Bar(
                symbol="ES",
                ts=start + timedelta(minutes=index),
                open=price,
                high=close + 0.2,
                low=price - 0.2,
                close=close,
                volume=1000,
                timeframe="1m",
            )
        )
        price = close
    return out


def test_option_reversal_waits_for_target_and_live_quote() -> None:
    bars = _bars()
    wait_target = _run_option_reversal(bars, [], OptionReversalParams())
    wait_live = _run_option_reversal(
        bars,
        [{"id": "opt-1", "point": {"price": bars[-1].close}}],
        OptionReversalParams(),
    )

    assert wait_target["latest"]["state"] == "WAIT_TARGET"
    assert wait_live["latest"]["state"] == "WAIT_LIVE"
    assert wait_live["targets"] == 1
    for result in (wait_target, wait_live):
        assert result["table"]["model_ref"] == "option_reversal"
        assert result["overlays"] == [
            {
                "type": "table",
                "source": "option_reversal",
                "control_key": "table",
                "table": result["table"],
                "tone": "warning",
                "scenario": "option_reversal",
                "trigger_event": result["latest"]["trigger_event"],
                "quality": {"code": "score"},
            }
        ]
    normalized = attach_indicator_status(
        "option_reversal",
        wait_live,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
    )
    manifest = indicator_manifest()["option_reversal"]
    assert "targets" in manifest["runtime_payload_contract"]["top_level"]
    validate_indicator_result_contract(
        normalized,
        name="option_reversal",
        renderer_contract=manifest["renderer_contract"],
        runtime_payload_contract=manifest["runtime_payload_contract"],
    )

    with pytest.raises(ValueError, match="confirmed closed 1m"):
        _run_option_reversal(
            [replace(bars[-1], closed=False, state="forming")],
            [],
            OptionReversalParams(),
        )
    with pytest.raises(ValueError, match="confirmed closed 1m"):
        _run_option_reversal([replace(bars[-1], timeframe="5m")], [], OptionReversalParams())


def test_option_reversal_buy_advisory_contract() -> None:
    bars = _bars()
    latest = bars[-1].close
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "timeframe": "1m",
        "point": {"price": latest},
        "right": "C",
        "strike": 5000,
        "local_symbol": "ES C5000",
        "reference_option_price": 10.0,
        "live_bid": 7.8,
        "live_ask": 8.0,
        "live_underlying_price": latest,
    }
    result = _run_option_reversal(bars, [target], OptionReversalParams())

    assert result["latest"]["state"] == "BUY"
    assert result["latest"]["action"] == "BUY_CALL"
    assert result["latest"]["direction"] == "long"
    assert result["latest"]["option_side"] == "CALL"
    assert result["latest"]["requirements"] == {"option_target": True, "live_option": True}
    assert result["latest"]["reason_code"] == "premium_compression_entry_candidate"
    assert result["latest"]["trigger_event"] == {"code": "premium_compression_entry_candidate"}
    assert "reason" not in result["latest"]
    assert "recommendation" not in result["latest"]
    assert result["latest"]["metrics"]["compression_ratio"] < 0.82
    assert result["latest"]["metrics"]["price_source"] == "bid_ask_mid"
    assert result["latest"]["monte_carlo_bridge"]["trigger_simulation"] is True
    assert result["table"]["model_ref"] == "option_reversal"
    assert result["table"]["target"] == result["latest"]["target"]
    assert result["table"]["metrics"] == result["latest"]["metrics"]
    assert result["table"]["trigger_event"] == {"code": "premium_compression_entry_candidate"}
    table_overlay = next(overlay for overlay in result["overlays"] if overlay["type"] == "table")
    assert table_overlay["control_key"] == "table"
    signal = result["latest"]["signal"]
    assert signal["code"] == "OPTION_REVERSAL_BUY_CALL"
    assert signal["action"] == "BUY_CALL"
    assert signal["direction"] == "long"
    assert signal["option_side"] == "CALL"
    assert all(overlay["type"] != "table" for overlay in signal["overlays"])
    assert result["events"] == [signal]
    assert result["signals"] == []
    assert result["series"] == []
    assert result["events"][0]["event_type"] == "option_reversal"
    observation = result["research_observation"]
    assert observation["schema_version"] == 2
    assert observation["action_taken"] == "BUY_CALL"
    assert observation["trigger_code"] == "premium_compression_entry_candidate"
    assert observation["contract"]["option_target_id"] == "opt-1"
    assert observation["contract"]["option_contract_id"] == "12345"
    assert observation["features"]["target_price"] == latest
    assert observation["features"]["atr"] == pytest.approx(0.45)
    assert set(observation["features"]) == set(OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS)
    assert observation["feature_availability"] == {
        "previous_pair": False,
        "bid_ask": True,
        "target_delta": False,
    }

    normalized = attach_indicator_status(
        "option_reversal",
        result,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
    )
    manifest = indicator_manifest()["option_reversal"]
    assert manifest["runtime_payload_compact"]["research_observation"] == {"drop": True}
    validate_indicator_result_contract(
        normalized,
        name="option_reversal",
        renderer_contract=manifest["renderer_contract"],
        runtime_payload_contract=manifest["runtime_payload_contract"],
    )


def test_option_reversal_uses_typed_live_underlying_between_closed_bars() -> None:
    bars = _bars()
    target = _target_fixture(
        {
            "id": "opt-live-underlying",
            "symbol": "ES",
            "timeframe": "1m",
            "point": {"price": 100.0},
            "right": "C",
            "reference_option_price": 8.0,
            "live_bid": 7.9,
            "live_ask": 8.1,
            "live_underlying_price": 100.0,
        }
    )

    result = _option_reversal(
        bars,
        option_targets=[target],
        params=OptionReversalParams(),
    )

    assert bars[-1].close != 100.0
    assert result["latest"]["metrics"]["near_atr"] == 0.0
    assert result["research_observation"]["features"]["underlying_price"] == 100.0


def test_option_reversal_observation_uses_the_selected_exact_point() -> None:
    bars = _bars()
    latest = bars[-1].close
    shared_quote = {
        "right": "C",
        "strike": 5000,
        "reference_option_price": 10.0,
        "live_bid": 7.8,
        "live_ask": 8.0,
    }

    result = _run_option_reversal(
        bars,
        [
            {
                "id": "far-point",
                "point": {"price": latest + 8.0},
                "live_underlying_price": latest + 20.0,
                **shared_quote,
            },
            {
                "id": "near-point",
                "point": {"price": latest},
                "live_underlying_price": latest + 1.0,
                **shared_quote,
            },
        ],
        OptionReversalParams(),
    )

    observation = result["research_observation"]
    assert observation["contract"]["option_target_id"] == "near-point"
    assert observation["features"]["target_price"] == latest
    assert observation["features"]["underlying_price"] == latest + 1.0


def test_option_reversal_uses_fair_target_price_for_compression() -> None:
    bars = _bars()
    latest = bars[-1].close
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "timeframe": "1m",
        "point": {"price": latest},
        "right": "P",
        "strike": 5000,
        "local_symbol": "ES P5000",
        "fair_price": 15.0,
        "reference_option_price": 12.9,
        "live_bid": 12.8,
        "live_ask": 13.1,
        "live_underlying_price": latest,
    }
    result = _run_option_reversal(bars, [target], OptionReversalParams())

    assert result["latest"]["state"] == "WATCH"
    assert result["latest"]["metrics"]["reference_price"] == 15.0
    assert result["latest"]["metrics"]["compression_ratio"] < 0.88
    overlay = result["overlays"][0]
    assert overlay["role"] == "option_reaction_zone"
    assert overlay["tone"] == "negative"
    assert overlay["style"] == "dashed"
    assert overlay["width"] == 1.6
    assert overlay["opacity"] == 0.0
    assert overlay["border_opacity"] == 0.84
    assert overlay["animated_border"] is True
    assert "fill_mode" not in overlay
    assert "bg" not in overlay
    assert "border" not in overlay


def test_option_reversal_requires_live_quote_for_signal() -> None:
    bars = _bars()
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "timeframe": "1m",
        "point": {"price": bars[-1].close},
        "right": "P",
        "reference_option_price": 10.0,
        "fair_price": 7.0,
    }
    result = _run_option_reversal(bars, [target], OptionReversalParams())

    assert result["latest"]["state"] == "WAIT_LIVE"
    assert result["latest"]["reason_code"] == "option_market_data_pending"
    assert result["latest"]["requirements"] == {
        "option_target": True,
        "live_option": False,
    }
    assert result.get("signals", []) == []


def test_option_reversal_stale_or_empty_quote_is_typed_wait_live() -> None:
    bars = _bars()
    target = _target_fixture(
        {
            "id": "opt-stale",
            "point": {"price": bars[-1].close},
            "right": "P",
            "live_bid": 7.9,
            "live_ask": 8.1,
            "live_underlying_price": bars[-1].close,
        }
    )
    for status in ("stale", "empty"):
        target["payload"]["market_sample"]["live_quote_status"] = status
        result = _option_reversal(
            bars,
            option_targets=[target],
            params=OptionReversalParams(),
        )
        assert result["latest"]["state"] == "WAIT_LIVE"
        assert result["latest"]["reason_code"] == "option_market_data_stale"
        assert result["latest"]["requirements"] == {
            "option_target": True,
            "live_option": False,
        }


def test_option_reversal_freshness_uses_explicit_analysis_clock() -> None:
    bars = _bars()
    target = _target_fixture(
        {
            "id": "opt-clock",
            "point": {"price": bars[-1].close},
            "right": "C",
            "reference_option_price": 10.0,
            "live_bid": 7.9,
            "live_ask": 8.0,
            "live_underlying_price": bars[-1].close,
        }
    )

    fresh = _option_reversal(
        bars,
        option_targets=[target],
        params=OptionReversalParams(),
        analysis_as_of_utc=OPTION_REVERSAL_TEST_AS_OF,
    )
    stale = _option_reversal(
        bars,
        option_targets=[target],
        params=OptionReversalParams(),
        analysis_as_of_utc=(OPTION_REVERSAL_TEST_AS_OF + timedelta(seconds=31)),
    )

    assert fresh["latest"]["state"] != "WAIT_LIVE"
    assert stale["latest"]["state"] == "WAIT_LIVE"
    assert stale["latest"]["reason_code"] == "option_market_data_stale"


def test_option_reversal_accepts_signed_futures_underlying_and_target() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(
            "CL",
            start + timedelta(minutes=index),
            -10.1,
            -9.8,
            -10.2,
            -10.0,
            1000.0,
            "1m",
        )
        for index in range(40)
    ]
    target = _target_fixture(
        {
            "id": "negative-fop",
            "point": {"price": -10.0},
            "right": "P",
            "reference_option_price": 2.0,
            "live_bid": 1.9,
            "live_ask": 2.1,
            "live_underlying_price": -10.0,
            "previous_reference_option_price": 2.05,
            "previous_underlying_price": -9.75,
        }
    )

    result = _option_reversal(
        bars,
        option_targets=[target],
        params=OptionReversalParams(),
        analysis_as_of_utc=OPTION_REVERSAL_TEST_AS_OF,
    )

    assert result["latest"]["state"] != "WAIT_LIVE"
    assert result["latest"]["target"]["target_price"] == -10.0
    assert result["latest"]["metrics"]["previous_underlying_price"] == -9.75
    assert result["research_observation"]["features"]["underlying_price"] == -10.0
    assert result["research_observation"]["feature_availability"]["previous_pair"] is True


def test_option_reversal_blocks_delayed_option_or_underlying_entitlement() -> None:
    bars = _bars()
    target = _target_fixture(
        {
            "id": "opt-delayed",
            "point": {"price": bars[-1].close},
            "right": "P",
            "reference_option_price": 10.0,
            "live_bid": 7.9,
            "live_ask": 8.1,
            "live_underlying_price": bars[-1].close,
        }
    )

    for field in ("live_quote_entitlement", "underlying_quote_entitlement"):
        sample = target["payload"]["market_sample"]
        original = sample[field]
        original_live_underlying = sample.get("live_underlying_price")
        sample[field] = "delayed"
        if field == "live_quote_entitlement":
            sample["live_quote_status"] = "delayed"
        else:
            sample["underlying_quote_status"] = "delayed"
            sample.pop("live_underlying_price", None)
        result = _option_reversal(
            bars,
            option_targets=[target],
            params=OptionReversalParams(),
        )
        assert result["latest"]["state"] == "WAIT_LIVE"
        sample[field] = original
        if original_live_underlying is not None:
            sample["live_underlying_price"] = original_live_underlying
        sample["live_quote_status"] = "ok"
        sample["underlying_quote_status"] = "live"


def test_option_reversal_blocks_previous_close_but_allows_typed_live_mid() -> None:
    bars = _bars()
    target = _target_fixture(
        {
            "id": "opt-ask",
            "point": {"price": bars[-1].close},
            "right": "C",
            "reference_option_price": 10.0,
            "live_bid": 7.9,
            "live_ask": 8.0,
            "live_underlying_price": bars[-1].close,
        }
    )

    allowed = _option_reversal(
        bars,
        option_targets=[target],
        params=OptionReversalParams(),
    )
    assert allowed["latest"]["state"] == "BUY"
    assert allowed["latest"]["metrics"]["price_source"] == "bid_ask_mid"

    target["payload"]["market_sample"]["reference_option_price_source"] = "previous_close"
    target["payload"]["market_sample"]["live_quote_status"] = "display_only"
    blocked = _option_reversal(
        bars,
        option_targets=[target],
        params=OptionReversalParams(),
    )
    assert blocked["latest"]["state"] == "WAIT_LIVE"


def test_option_reversal_contract_has_no_gex_input() -> None:
    assert "gex_history" not in inspect.signature(_option_reversal).parameters
    manifest = indicator_manifest()["option_reversal"]
    assert manifest["execution_lanes"] == ["primary", "fast"]
    assert manifest["fast_trigger_refs"] == ["option_target_sample"]
    assert manifest["paper_tradable"] is False
    assert manifest["confirmed_bar_context"] == [
        {
            "timeframe": "1m",
            "history_bars": 256,
            "role": "lower_timeframe_confirmation",
        }
    ]


def test_option_reversal_softens_compression_score_for_wide_spread() -> None:
    bars = _bars()
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "timeframe": "1m",
        "point": {"price": bars[-1].close},
        "right": "P",
        "reference_option_price": 10.0,
        "live_bid": 8.0,
        "live_ask": 12.0,
        "live_underlying_price": bars[-1].close,
    }
    result = _run_option_reversal(bars, [target], OptionReversalParams())

    assert result["latest"]["metrics"]["compression_ratio"] == 1.0
    assert result["latest"]["metrics"]["spread_ratio"] == 0.4
    assert result["latest"]["metrics"]["compression_score"] > 0
    assert result["latest"]["state"] == "WAIT"
    assert result.get("signals", []) == []


def test_option_reversal_detects_put_premium_lag_after_underlying_reaction() -> None:
    bars = _bars()
    latest = bars[-1].close
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "timeframe": "1m",
        "point": {"price": latest},
        "right": "P",
        "strike": 5000,
        "local_symbol": "ES P5000",
        "reference_option_price": 8.05,
        "live_bid": 7.9,
        "live_ask": 8.05,
        "live_underlying_price": latest,
        "previous_reference_option_price": 8.0,
        "previous_underlying_price": latest + 0.25,
    }
    result = _run_option_reversal(bars, [target], OptionReversalParams())

    assert result["latest"]["state"] == "BUY"
    assert result["latest"]["action"] == "BUY_PUT"
    assert result["latest"]["direction"] == "short"
    assert result["latest"]["option_side"] == "PUT"
    assert result["latest"]["metrics"]["compression_score"] < 0.5
    assert result["latest"]["metrics"]["premium_lag_edge"] is True
    assert result["latest"]["metrics"]["lag_score"] >= 0.65
    assert result["latest"]["reason_code"] == "premium_lag_entry_candidate"
    assert result["latest"]["trigger_event"] == {"code": "premium_lag_entry_candidate"}
    assert result["overlays"][0]["type"] == "box"
    assert result["overlays"][0]["role"] == "option_reaction_zone"
    assert result["overlays"][0]["animated_border"] is True
    assert result["overlays"][0]["direction"] == "short"
    assert result["overlays"][0]["option_side"] == "PUT"


def test_option_reversal_does_not_treat_current_underlying_as_previous_sample() -> None:
    bars = _bars()
    latest = bars[-1].close
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "timeframe": "1m",
        "point": {"price": latest},
        "right": "P",
        "strike": 5000,
        "local_symbol": "ES P5000",
        "reference_option_price": 8.05,
        "live_bid": 7.9,
        "live_ask": 8.05,
        "live_underlying_price": latest,
        "underlying_price": latest + 0.25,
    }
    result = _run_option_reversal(bars, [target], OptionReversalParams())

    assert result["latest"]["metrics"]["previous_underlying_price"] is None
    assert result["latest"]["metrics"]["premium_lag_edge"] is False
    assert result.get("signals", []) == []
