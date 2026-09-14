from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.gex.option_target_valuation import (
    _apply_option_live_quote,
    _market_option_reference,
    _option_reference_price,
    _refresh_fair_from_market_quote,
)
from aef_terminal.data.gex.option_target_contract import (
    option_target_market_sample,
    require_option_target_underlying_quote,
)


def test_option_live_quote_requires_typed_option_model_before_provider_call() -> None:
    calls = []
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
        },
        lambda contract: calls.append(contract),
    )

    assert calls == []
    assert result["live_quote_status"] == "error"
    assert "OPT/FOP" in result["live_quote_message"]


def test_option_live_quote_does_not_accept_trade_price_alias() -> None:
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "trade_price": 2.5,
            "ts": "2026-07-22T12:00:00+00:00",
        },
    )

    assert result["live_quote_status"] == "error"
    assert result["reference_option_price"] is None
    assert result["reference_option_price_source"] == "unavailable"
    assert result["live_last"] is None


def test_option_live_quote_keeps_delayed_last_visual_only() -> None:
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "price": 2.5,
            "price_source": "last",
            "last": 2.5,
            "ts": "2026-07-22T12:00:00+00:00",
            "provider_ts": "2026-07-22T12:00:00+00:00",
            "time_basis": "provider_event",
            "market_data_type": 3,
            "market_data_entitlement": "delayed",
        },
    )

    assert result["live_quote_status"] == "delayed"
    assert result["live_quote_entitlement"] == "delayed"
    assert result["reference_option_price"] == 2.5
    assert result["live_quote_time_basis"] == "provider_event"


def test_option_live_quote_rejects_duplicate_gex_reference_fields() -> None:
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "reference_option_price": 99.0,
            "reference_option_price_source": "last",
            "price": 2.5,
            "price_source": "bid_ask_mid",
            "bid": 2.4,
            "ask": 2.6,
            "last": 2.7,
            "ts": "2026-07-22T12:00:00+00:00",
            "provider_ts": "2026-07-22T12:00:00+00:00",
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["live_quote_status"] == "error"
    assert result["reference_option_price"] is None
    assert result["reference_option_price_source"] == "unavailable"


def test_option_live_quote_marks_close_reference_display_only() -> None:
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "price": 2.5,
            "price_source": "previous_close",
            "close": 2.5,
            "ts": "2026-07-22T12:00:00+00:00",
            "provider_ts": "2026-07-22T12:00:00+00:00",
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["live_quote_status"] == "display_only"
    assert result["live_quote_entitlement"] == "live"


def test_option_live_quote_marks_ibkr_market_price_display_only() -> None:
    captured_at = "2026-07-22T12:00:00+00:00"
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "price": 2.5,
            "price_source": "ibkr_market_price",
            "ts": captured_at,
            "provider_ts": captured_at,
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["live_quote_status"] == "display_only"
    assert result["reference_option_price"] == 2.5


def test_option_live_quote_uses_client_receive_time_without_claiming_provider_time() -> None:
    received_at = "2026-07-22T12:00:00+00:00"
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "price": 2.5,
            "price_source": "bid_ask_mid",
            "bid": 2.4,
            "ask": 2.6,
            "ts": received_at,
            "provider_ts": None,
            "received_at": received_at,
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["live_quote_status"] == "ok"
    assert result["live_quote_ts"] == received_at
    assert result["live_quote_time_basis"] == "client_receive"


def test_option_live_quote_rejects_timestamp_alias_disagreement() -> None:
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "price": 2.5,
            "price_source": "last",
            "last": 2.5,
            "ts": "2026-07-22T12:00:01+00:00",
            "provider_ts": "2026-07-22T12:00:00+00:00",
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["live_quote_status"] == "error"
    assert result["reference_option_price"] is None


def test_option_live_quote_never_admits_last_for_calculation() -> None:
    observed_at = "2026-07-22T12:00:00+00:00"
    result = _apply_option_live_quote(
        {
            "ok": True,
            "con_id": 123,
            "exchange": "SMART",
            "provider_symbol": "SPY",
            "futures_options": False,
        },
        lambda _contract: {
            "price": 2.5,
            "price_source": "last",
            "last": 2.5,
            "ts": observed_at,
            "provider_ts": observed_at,
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["live_quote_status"] == "display_only"
    assert result["reference_option_price"] == 2.5
    assert result["reference_option_price_source"] == "last"


def test_fair_repricing_requires_authoritative_live_quote() -> None:
    for status in ("display_only", "frozen", "delayed", "delayed_frozen", "unknown"):
        payload = {
            "ok": True,
            "live_quote_status": status,
            "reference_option_price": 2.5,
            "reference_option_price_source": "last",
            "fair_price": 1.75,
        }

        result = _refresh_fair_from_market_quote(payload)

        assert result is not payload
        assert result["fair_price"] is None
        assert result["display_fair_price"] == 1.75
        assert result["fair_price_status"] == "stale"


def test_fair_repricing_requires_joint_current_underlying_quote() -> None:
    observed_at = "2026-07-22T12:00:00+00:00"
    payload = {
        "ok": True,
        "futures_options": False,
        "right": "C",
        "strike": 500.0,
        "target_underlying_price": 501.0,
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "live_quote_status": "ok",
        "live_quote_entitlement": "live",
        "live_quote_time_basis": "client_receive",
        "live_quote_ts": observed_at,
        "reference_option_price": 2.5,
        "reference_option_price_source": "bid_ask_mid",
        "live_bid": 2.4,
        "live_ask": 2.6,
        "fair_price": 1.75,
    }

    result = _refresh_fair_from_market_quote(
        payload,
        valuation_now=datetime.fromisoformat(observed_at),
    )

    assert result["fair_price"] is None
    assert result["display_fair_price"] == 1.75
    assert result["fair_price_status"] == "stale"
    assert "option/underlying quote pair" in result["fair_price_message"]


def test_stale_fair_market_sample_keeps_only_typed_display_value() -> None:
    sample = option_target_market_sample(
        {
            "fair_price": None,
            "display_fair_price": 1.75,
            "fair_price_status": "stale",
            "fair_price_message": "Fair price stale.",
        }
    )

    assert "fair_price" not in sample
    assert sample["display_fair_price"] == 1.75
    assert sample["fair_price_status"] == "stale"


def test_futures_underlying_quote_and_market_sample_preserve_signed_prices() -> None:
    observed_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    observed_text = observed_at.isoformat()
    quote = require_option_target_underlying_quote(
        {
            "price": -10.0,
            "price_source": "last",
            "last": -10.0,
            "bid": -10.25,
            "ask": -9.75,
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
            "market_data_type": 1,
            "market_data_entitlement": "live",
            "provider_ts": observed_text,
            "received_at": observed_text,
            "ts": observed_text,
            "time_basis": "provider_event",
        },
        observed_at=observed_at,
        futures_options=True,
    )

    assert quote["price"] == -10.0
    assert quote["execution_price"] == -10.0

    sample = option_target_market_sample(
        {
            "reference_option_price": 2.0,
            "reference_option_price_source": "bid_ask_mid",
            "live_bid": 1.9,
            "live_ask": 2.1,
            "live_quote_status": "ok",
            "live_quote_entitlement": "live",
            "live_quote_ts": observed_text,
            "live_quote_time_basis": "client_receive",
            "live_underlying_price": -10.0,
            "underlying_quote_price": -10.0,
            "underlying_quote_price_source": "last",
            "underlying_quote_entitlement": "live",
            "underlying_quote_status": "live",
            "underlying_quote_ts": observed_text,
            "underlying_quote_time_basis": "provider_event",
        },
        analysis_as_of_utc=observed_at,
        sec_type="FOP",
    )

    assert sample["live_underlying_price"] == -10.0
    assert sample["underlying_quote_price"] == -10.0

    with pytest.raises(ValueError, match="finite positive"):
        require_option_target_underlying_quote(
            {
                "price": -10.0,
                "price_source": "last",
            },
            observed_at=observed_at,
            futures_options=False,
        )
    with pytest.raises(ValueError, match="live_underlying_price must be finite and positive"):
        option_target_market_sample(
            {"live_underlying_price": -10.0},
            sec_type="OPT",
        )


def test_underlying_quote_uses_fresh_bbo_when_selected_last_is_stale() -> None:
    observed_at = datetime(2026, 7, 22, 12, 0, 10, tzinfo=UTC)
    stale_last_at = observed_at - timedelta(seconds=8)
    fresh_bbo_at = observed_at - timedelta(seconds=1)

    quote = require_option_target_underlying_quote(
        {
            "price": 100.0,
            "price_source": "last",
            "last": 100.0,
            "bid": 100.25,
            "ask": 100.75,
            "status": "stale",
            "is_stale": True,
            "is_delayed": False,
            "market_data_type": 1,
            "market_data_entitlement": "live",
            "provider_ts": stale_last_at.isoformat(),
            "last_provider_ts": stale_last_at.isoformat(),
            "received_at": fresh_bbo_at.isoformat(),
            "bid_ask_received_at": fresh_bbo_at.isoformat(),
            "bid_ask_status": "live",
            "ts": stale_last_at.isoformat(),
            "time_basis": "provider_event",
        },
        observed_at=observed_at,
        futures_options=True,
    )

    assert quote["price"] == 100.5
    assert quote["price_source"] == "bid_ask_mid"
    assert quote["status"] == "live"
    assert quote["is_stale"] is False
    assert quote["time_basis"] == "client_receive"
    assert quote["quote_ts"] == fresh_bbo_at.isoformat()
    assert quote["execution_price"] == 100.5


def test_underlying_quote_does_not_rescue_stale_last_with_stale_bbo() -> None:
    observed_at = datetime(2026, 7, 22, 12, 0, 10, tzinfo=UTC)
    stale_at = observed_at - timedelta(seconds=8)

    quote = require_option_target_underlying_quote(
        {
            "price": 100.0,
            "price_source": "last",
            "last": 100.0,
            "bid": 100.25,
            "ask": 100.75,
            "status": "stale",
            "is_stale": True,
            "is_delayed": False,
            "market_data_type": 1,
            "market_data_entitlement": "live",
            "provider_ts": stale_at.isoformat(),
            "last_provider_ts": stale_at.isoformat(),
            "received_at": stale_at.isoformat(),
            "bid_ask_received_at": stale_at.isoformat(),
            "bid_ask_status": "stale",
            "ts": stale_at.isoformat(),
            "time_basis": "provider_event",
        },
        observed_at=observed_at,
        futures_options=True,
    )

    assert quote["price_source"] == "last"
    assert quote["status"] == "stale"
    assert quote["is_stale"] is True
    assert quote["execution_price"] is None


def test_market_option_reference_reads_only_the_selected_typed_fact() -> None:
    assert (
        _market_option_reference(
            {
                "reference_option_price": 5.0,
                "reference_option_price_source": "ibkr_market_price",
            }
        )
        == 5.0
    )
    assert (
        _market_option_reference(
            {
                "live_mid": 5.0,
                "live_bid": 4.0,
                "live_ask": 6.0,
                "live_last": 5.0,
            }
        )
        is None
    )


def test_cached_option_reference_reads_only_the_producer_selected_fact() -> None:
    row = {
        "reference_option_price": None,
        "reference_option_price_source": "unavailable",
        "bid": 4.0,
        "ask": 6.0,
        "last": 5.0,
    }

    assert _option_reference_price(row) is None


def test_cached_option_reference_rejects_legacy_option_price() -> None:
    row = {
        "option_price": 5.0,
        "reference_option_price": 5.0,
        "reference_option_price_source": "last",
    }

    try:
        _option_reference_price(row)
    except ValueError as exc:
        assert "deleted legacy option_price" in str(exc)
    else:
        raise AssertionError("legacy option_price was accepted")
