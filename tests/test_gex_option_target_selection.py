from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from aef_terminal.data.gex import math as gex_math
from aef_terminal.data.gex import option_targets as gex_option_targets
from aef_terminal.data.gex.constants import NY_TZ
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.domain import OptionRight
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


# Compose only the concrete calculation owners exercised by this test module.
gex = SimpleNamespace(
    NY_TZ=NY_TZ,
    _option_model_gamma_delta=gex_math._option_model_gamma_delta,
    _option_model_price=gex_math._option_model_price,
    option_target_price=gex_option_targets.option_target_price,
)


_ES = ibkr_future_payload("ES")
_SPY = ibkr_stock_payload("SPY", con_id=756733)
_QQQ = ibkr_stock_payload("QQQ", con_id=320227571)
_OPTION_TEST_UNIVERSES: dict[str, dict[str, Any]] = {}


def _gex_test_spot_observation(
    spot: float,
    *,
    entitlement: str = "live",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    market_data_type = {
        "live": 1,
        "frozen": 2,
        "delayed": 3,
        "delayed_frozen": 4,
    }.get(entitlement)
    if observed_at is not None and entitlement == "live":
        return {
            "price": float(spot),
            "price_source": "bid_ask_mid",
            "bid": float(spot) - 0.5,
            "ask": float(spot) + 0.5,
            "last": None,
            "received_at": observed_at.astimezone(UTC).isoformat(),
            "provider_ts": None,
            "time_basis": "client_receive",
            "market_data_type": market_data_type,
            "market_data_entitlement": entitlement,
        }
    return {
        "price": float(spot),
        "price_source": "ibkr_market_price",
        "bid": None,
        "ask": None,
        "last": None,
        "received_at": None,
        "provider_ts": None,
        "time_basis": None,
        "market_data_type": market_data_type,
        "market_data_entitlement": entitlement,
    }


def _option_identity_kwargs(
    instrument: dict[str, Any],
    premium_cap: float = 14.0,
) -> dict[str, Any]:
    instrument_id = qualified_instrument_id(instrument)

    def option_contract_provider(**kwargs: Any) -> dict[str, Any]:
        universe = _OPTION_TEST_UNIVERSES[instrument_id]
        fixed_contract = kwargs.get("fixed_contract")
        if not isinstance(fixed_contract, dict):
            return universe
        con_id = parse_exact_positive_decimal_provider_id(fixed_contract.get("con_id"))
        if any(
            parse_exact_positive_decimal_provider_id(row.get("con_id")) == con_id
            for row in universe["contracts"]
        ):
            return universe
        expiry = str(fixed_contract.get("expiry") or "")
        expiry_date = datetime.strptime(expiry, "%Y%m%d")
        expiry_at = (
            datetime(
                expiry_date.year,
                expiry_date.month,
                expiry_date.day,
                16,
                0,
                tzinfo=gex.NY_TZ,
            )
            .astimezone(UTC)
            .isoformat()
        )
        futures_options = instrument.get("asset_class") == "future"
        return {
            **universe,
            "contracts": [
                {
                    **fixed_contract,
                    "con_id": con_id,
                    "sec_type": "FOP" if futures_options else "OPT",
                    "expiry_at": expiry_at,
                    "trading_class": str(
                        fixed_contract.get("trading_class") or instrument["provider_symbol"]
                    ),
                    "multiplier": float(
                        fixed_contract.get("multiplier") or (50.0 if futures_options else 100.0)
                    ),
                    "currency": str(fixed_contract.get("currency") or "USD"),
                }
            ],
        }

    return {
        "instrument": instrument,
        "premium_caps": {instrument_id: premium_cap},
        "option_contract_provider": option_contract_provider,
    }


def _install_option_universe(
    monkeypatch: Any,
    *,
    instrument: dict[str, Any],
    provider_symbol: str,
    captured_at: datetime,
    meta: dict[str, Any],
    contracts: list[dict[str, Any]],
) -> dict[str, Any]:
    futures_options = instrument.get("asset_class") == "future"
    used_con_ids: set[int] = set()
    next_con_id = 100001
    qualified_contracts: list[dict[str, Any]] = []
    for source_row in contracts:
        row = dict(source_row)
        con_id = row.get("con_id")
        if not isinstance(con_id, int) or isinstance(con_id, bool) or con_id <= 0:
            while next_con_id in used_con_ids:
                next_con_id += 1
            con_id = next_con_id
            next_con_id += 1
        used_con_ids.add(con_id)
        expiry = str(row["expiry"])
        expiry_date = datetime.strptime(expiry, "%Y%m%d")
        expiry_at = row.get("expiry_at")
        if not isinstance(expiry_at, str) or not expiry_at:
            expiry_at = (
                datetime(
                    expiry_date.year,
                    expiry_date.month,
                    expiry_date.day,
                    16,
                    0,
                    tzinfo=gex.NY_TZ,
                )
                .astimezone(UTC)
                .isoformat()
            )
        qualified_contracts.append(
            {
                "con_id": con_id,
                "sec_type": "FOP" if futures_options else "OPT",
                "expiry": expiry,
                "expiry_at": expiry_at,
                "strike": float(row["strike"]),
                "right": row["right"],
                "multiplier": float(row.get("multiplier") or (50.0 if futures_options else 100.0)),
                "trading_class": str(row.get("trading_class") or provider_symbol),
                "exchange": str(row.get("exchange") or ("CME" if futures_options else "SMART")),
                "currency": str(row.get("currency") or "USD"),
                "local_symbol": str(row.get("local_symbol") or ""),
            }
        )
    theoretical_iv = next(
        (
            float(row["iv"])
            for row in contracts
            if isinstance(row.get("iv"), (int, float))
            and not isinstance(row.get("iv"), bool)
            and float(row["iv"]) > 0
        ),
        0.20,
    )
    universe = {
        "source": "test_qualified_option_chain",
        "qualified_at": captured_at.astimezone(UTC).isoformat(),
        "instrument_id": qualified_instrument_id(instrument),
        "route_fingerprint": route_fingerprint(instrument),
        "contracts": qualified_contracts,
        "meta": {
            "spot": float(meta["spot"]),
            "futures_options": futures_options,
            "theoretical_iv": theoretical_iv,
            "risk_free_rate": float(meta.get("risk_free_rate", 0.05)),
            "dividend_yield": float(meta.get("dividend_yield", 0.0)),
            "market_data_requested": False,
            "pricing_basis": "theoretical_chain",
        },
    }
    monkeypatch.setitem(
        _OPTION_TEST_UNIVERSES,
        qualified_instrument_id(instrument),
        universe,
    )
    return universe


def test_option_target_price_requires_typed_exact_instrument_premium_cap() -> None:
    result = gex.option_target_price(
        instrument=_ES,
        target_price=4990,
        premium_caps={},
    )

    assert result == {
        "ok": False,
        "status": "configuration_required",
        "code": "OPTION_PREMIUM_CAP_REQUIRED",
        "configuration": {
            "field": "premium_cap",
            "scope": "instrument_id",
        },
        "message": "Option premium cap configuration is required.",
        "instrument_id": qualified_instrument_id(_ES),
        "route_fingerprint": route_fingerprint(_ES),
        "provider": "ibkr",
        "provider_symbol": "ES",
    }


def test_option_target_request_preserves_signed_futures_target_only() -> None:
    futures_request, futures_error = gex_option_targets._prepare_option_target_request(
        instrument=_ES,
        target_price=-10.0,
        target_ts=None,
        right="P",
        mode="normal",
        dte="0dte",
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        premium_caps={qualified_instrument_id(_ES): 20.0},
    )
    stock_request, stock_error = gex_option_targets._prepare_option_target_request(
        instrument=_SPY,
        target_price=-10.0,
        target_ts=None,
        right="P",
        mode="normal",
        dte="0dte",
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        premium_caps={qualified_instrument_id(_SPY): 20.0},
    )

    assert futures_error is None
    assert futures_request is not None
    assert futures_request.target == -10.0
    assert futures_request.futures_options is True
    assert stock_request is None
    assert stock_error is not None
    assert stock_error["message"] == (
        "target_price must be finite and positive for non-futures instruments."
    )


def test_option_target_price_prefers_contract_under_asset_premium_cap(monkeypatch) -> None:
    valuation_now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=valuation_now,
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 5010,
                "right": "C",
                "gamma": 0.01,
                "delta": 0.46,
                "iv": 0.18,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 18.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 18,
                "ask": 19,
                "last": 18.5,
                "con_id": 1,
                "local_symbol": "ES C5010",
            },
            {
                "expiry": "20260602",
                "strike": 5020,
                "right": "C",
                "gamma": 0.02,
                "delta": 0.45,
                "iv": 0.18,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 14.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 2000,
                "abs_gex": 2000,
                "bid": 13.5,
                "ask": 14.5,
                "last": 14.0,
                "con_id": 2,
                "local_symbol": "ES C5020",
            },
            {
                "expiry": "20260602",
                "strike": 5030,
                "right": "C",
                "gamma": 0.015,
                "delta": 0.41,
                "iv": 0.18,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 12.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 1500,
                "abs_gex": 1500,
                "bid": 12.5,
                "ask": 13.0,
                "last": 12.8,
                "con_id": 4,
                "local_symbol": "ES C5030",
            },
            {
                "expiry": "20260602",
                "strike": 5040,
                "right": "C",
                "gamma": 0.01,
                "iv": 0.18,
                "reference_option_price": 10.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 1200,
                "abs_gex": 1200,
                "con_id": 5,
                "local_symbol": "ES C5040",
            },
            {
                "expiry": "20260602",
                "strike": 5050,
                "right": "C",
                "gamma": 0.01,
                "iv": 0.18,
                "reference_option_price": 8.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 1100,
                "abs_gex": 1100,
                "con_id": 6,
                "local_symbol": "ES C5050",
            },
            {
                "expiry": "20260602",
                "strike": 5060,
                "right": "C",
                "gamma": 0.01,
                "iv": 0.18,
                "reference_option_price": 6.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "con_id": 7,
                "local_symbol": "ES C5060",
            },
            {
                "expiry": "20260602",
                "strike": 5070,
                "right": "C",
                "gamma": 0.01,
                "iv": 0.18,
                "reference_option_price": 4.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 900,
                "abs_gex": 900,
                "con_id": 8,
                "local_symbol": "ES C5070",
            },
            {
                "expiry": "20260602",
                "strike": 4990,
                "right": "P",
                "gamma": 0.03,
                "delta": -0.44,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 17.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 3000,
                "abs_gex": 3000,
                "bid": 16.5,
                "ask": 17.5,
                "last": 17.0,
                "con_id": 3,
                "local_symbol": "ES P4990",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=valuation_now,
    )
    current_time_result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=valuation_now,
        now=valuation_now,
    )

    assert result["ok"] is True
    assert result["right"] == "C"
    assert result["strike"] == 5020.0
    assert result["iv"] == 0.18
    assert result["iv_source"] == "theoretical_default"
    assert result["pricing_basis"] == "theoretical_chain"
    assert result["subscription_state"] == "not_subscribed"
    assert result["premium_cap"] == 14.0
    assert result["mode"] == "normal"
    assert result["delta_band"] == [0.20, 0.42]
    assert result["reference_option_price"] <= 14.0
    assert "theoretical_price" not in result
    assert result["fair_price"] > 0
    assert result["max_limit_price"] > 0
    assert result["iv_scenarios"]["iv_down_10"] <= result["iv_scenarios"]["iv_up_10"]
    assert result["target_delta"] is not None
    assert result["target_gamma"] is not None
    assert result["target_gamma"] > 0
    assert result["alternatives"]
    assert len(result["alternatives"]) == 7
    assert [item["strike"] for item in result["alternatives"]] == [
        5010.0,
        5020.0,
        5030.0,
        5040.0,
        5050.0,
        5060.0,
        5070.0,
    ]
    assert len({item["con_id"] for item in result["alternatives"]}) == 7
    assert any(item["over_cap_by"] > 0 for item in result["alternatives"])
    assert any(item["over_cap_by"] == 0 for item in result["alternatives"])
    assert all(item["target_delta"] is not None for item in result["alternatives"])
    assert all(item["target_gamma"] is not None for item in result["alternatives"])
    assert result["theta_horizon_seconds"] == 1800.0
    assert datetime.fromisoformat(result["valuation_ts"]).astimezone(UTC) == datetime(
        2026,
        6,
        1,
        14,
        30,
        tzinfo=UTC,
    )
    assert result["time_to_expiry_years"] < current_time_result["time_to_expiry_years"]
    assert result["fair_price"] < current_time_result["fair_price"]


def test_option_target_fair_and_target_greeks_share_model_spot(monkeypatch) -> None:
    valuation_now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=valuation_now,
        meta={
            "spot": 5000.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 5010.0,
                "right": "C",
                "delta": 0.46,
                "iv": 0.18,
                "reference_option_price": 12.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 1_000.0,
                "bid": 11.5,
                "ask": 12.5,
                "last": 12.0,
                "con_id": 1,
                "local_symbol": "ES C5010",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=5001.0,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=valuation_now,
        right="C",
        strike=5010.0,
        expiry="20260602",
        con_id=1,
        local_symbol="ES C5010",
        exchange="CME",
    )

    years = result["time_to_expiry_years"]
    expected_fair = gex._option_model_price(
        True,
        spot=5000.0,
        strike=5010.0,
        years=years,
        iv=0.18,
        right=OptionRight.CALL,
        risk_free_rate=0.05,
        dividend_yield=0.0,
    )
    expected_greeks = gex._option_model_gamma_delta(
        True,
        spot=5000.0,
        strike=5010.0,
        years=years,
        iv=0.18,
        right=OptionRight.CALL,
        risk_free_rate=0.05,
        dividend_yield=0.0,
    )

    assert result["ok"] is True
    assert result["target_underlying_price"] == 5001.0
    assert expected_fair is not None
    assert expected_greeks is not None
    assert result["fair_price"] == round(expected_fair, 4)
    assert result["target_delta"] == round(expected_greeks["delta"], 4)
    assert result["target_gamma"] == expected_greeks["gamma"]


def test_option_target_price_classifies_dte_from_qualified_contract_expiry(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 5030,
                "right": "C",
                "gamma": 0.015,
                "delta": 0.41,
                "iv": 0.18,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 12.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 1500,
                "abs_gex": 1500,
                "bid": 12.5,
                "ask": 13.0,
                "last": 12.8,
                "con_id": 4,
                "local_symbol": "ES C5030",
            },
        ],
    )

    zero_dte = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        dte="0dte",
    )
    one_dte = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        dte="1dte",
    )

    assert zero_dte["ok"] is False
    assert zero_dte["target_dte"] == "0dte"
    assert "0DTE" in zero_dte["message"]
    assert zero_dte["available_expiries"] == ["20260602"]
    assert one_dte["ok"] is True
    assert one_dte["target_dte"] == "1dte"
    assert one_dte["expiry"] == "20260602"


def test_option_target_price_uses_next_provider_expiry_across_calendar_gap(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260603",
                "strike": 4975,
                "right": "P",
                "gamma": 0.015,
                "delta": -0.34,
                "iv": 0.18,
                "time_to_expiry_years": 0.006,
                "reference_option_price": 11.8,
                "open_interest": 100,
                "volume": 20,
                "gex": 1500,
                "abs_gex": 1500,
                "bid": 11.5,
                "ask": 12.0,
                "last": 11.8,
                "con_id": 44,
                "local_symbol": "ES P4975",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES, 100.0),
        target_price=5000,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        right="P",
        dte="1dte",
    )

    assert result["ok"] is True
    assert result["target_dte"] == "1dte"
    assert result["requested_expiry"] == "20260603"
    assert result["expiry"] == "20260603"


def test_option_target_price_starts_1dte_two_strikes_farther_otm(monkeypatch) -> None:
    contracts = []
    for index, strike in enumerate([5000, 4995, 4990, 4985, 4980, 4975]):
        contracts.append(
            {
                "expiry": "20260602",
                "strike": strike,
                "right": "P",
                "gamma": 0.02,
                "delta": -0.30,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 12.0 + index,
                "open_interest": 100,
                "volume": 20,
                "gex": -1000,
                "abs_gex": 1000,
                "bid": 11.75 + index,
                "ask": 12.25 + index,
                "last": 12.0 + index,
                "con_id": 100 + index,
                "local_symbol": f"ES P{strike}",
            }
        )
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=contracts,
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=5000,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        right="P",
        dte="1dte",
    )

    assert result["ok"] is True
    assert result["strike"] <= 4990
    assert all(item["strike"] <= 4990 for item in result["alternatives"])
    assert all(item["target_dte"] == "1dte" for item in result["alternatives"])
    assert all(item["expiry"] == "20260602" for item in result["alternatives"])
    assert all(item["fair_price"] > 0 for item in result["alternatives"])
    assert all(item["max_limit_price"] > item["fair_price"] for item in result["alternatives"])


def test_option_target_price_returns_nearby_alternatives_when_cap_filters_all(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4990,
                "right": "P",
                "gamma": 0.03,
                "delta": -0.46,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 21.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 3000,
                "abs_gex": 3000,
                "bid": 20.5,
                "ask": 21.5,
                "last": 21.0,
                "con_id": 3,
                "local_symbol": "ES P4990",
            },
            {
                "expiry": "20260602",
                "strike": 4980,
                "right": "P",
                "gamma": 0.02,
                "delta": -0.38,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 18.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 2000,
                "abs_gex": 2000,
                "bid": 18.0,
                "ask": 19.0,
                "last": 18.5,
                "con_id": 4,
                "local_symbol": "ES P4980",
            },
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 5,
                "exchange": "CME",
                "local_symbol": "ES P4970",
            },
            {
                "expiry": "20260602",
                "strike": 4960,
                "right": "P",
                "gamma": 0.018,
                "delta": -0.34,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 17.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 900,
                "abs_gex": 900,
                "bid": 16.5,
                "ask": 17.5,
                "last": 17.0,
                "con_id": 6,
                "local_symbol": "ES P4960",
            },
            {
                "expiry": "20260602",
                "strike": 4950,
                "right": "P",
                "gamma": 0.017,
                "delta": -0.33,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 22.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 800,
                "abs_gex": 800,
                "bid": 21.5,
                "ask": 22.5,
                "last": 22.0,
                "con_id": 7,
                "local_symbol": "ES P4950",
            },
            {
                "expiry": "20260602",
                "strike": 4940,
                "right": "P",
                "gamma": 0.016,
                "delta": -0.32,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 24.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 700,
                "abs_gex": 700,
                "bid": 23.5,
                "ask": 24.5,
                "last": 24.0,
                "con_id": 8,
                "local_symbol": "ES P4940",
            },
            {
                "expiry": "20260602",
                "strike": 4930,
                "right": "P",
                "gamma": 0.015,
                "delta": -0.31,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 26.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 600,
                "abs_gex": 600,
                "bid": 25.5,
                "ask": 26.5,
                "last": 26.0,
                "con_id": 9,
                "local_symbol": "ES P4930",
            },
            {
                "expiry": "20260602",
                "strike": 4920,
                "right": "P",
                "gamma": 0.014,
                "delta": -0.30,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 28.0,
                "open_interest": 100,
                "volume": 20,
                "gex": 500,
                "abs_gex": 500,
                "bid": 27.5,
                "ask": 28.5,
                "last": 28.0,
                "con_id": 10,
                "local_symbol": "ES P4920",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES, 0.01),
        target_price=5010,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    )

    assert result["ok"] is False
    assert result["right"] == "P"
    assert result["premium_cap"] == 0.01
    assert result["requested_expiry"] == "20260602"
    assert result["alternatives"]
    assert len(result["alternatives"]) == 7
    strikes = [item["strike"] for item in result["alternatives"]]
    assert strikes == sorted(strikes)
    assert result["alternatives"][0]["fair_price"] > 0
    estimated = {item["strike"]: item for item in result["alternatives"]}
    assert 4970.0 in estimated
    assert estimated[4970.0]["estimated_greeks"] is True
    assert estimated[4970.0]["delta"] < 0


def test_option_target_preview_does_not_use_chain_market_price_or_broker_greeks(
    monkeypatch,
) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_QQQ,
        provider_symbol="QQQ",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 744.07,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 744,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 2.83,
                "open_interest": 1000,
                "volume": 5276,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 2.82,
                "ask": 2.84,
                "last": 2.83,
                "con_id": 1,
                "local_symbol": "QQQ P744",
            },
            {
                "expiry": "20260602",
                "strike": 745,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 3.30,
                "open_interest": 1200,
                "volume": 5541,
                "gex": 1200,
                "abs_gex": 1200,
                "bid": 3.28,
                "ask": 3.31,
                "last": 3.29,
                "con_id": 2,
                "local_symbol": "QQQ P745",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_QQQ, 5.0),
        target_price=745.87,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    )

    assert result["ok"] is True
    assert result["right"] == "P"
    assert result["pricing_basis"] == "theoretical_chain"
    assert result["subscription_state"] == "not_subscribed"
    assert result["reference_option_price_source"] == "theoretical_model"
    assert result["iv_source"] == "theoretical_default"
    assert result["estimated_greeks"] is True
    assert result["liquidity"]["has_live_market"] is False
    assert result["liquidity"]["bid"] is None
    assert result["liquidity"]["ask"] is None
    assert result["fair_price"] > 0
    assert "theoretical_price" not in result


def test_option_target_price_can_reprice_fixed_contract(monkeypatch) -> None:
    valuation_now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    option_quote = {
        "price": 13.5,
        "price_source": "bid_ask_mid",
        "bid": 13.2,
        "ask": 13.8,
        "last": 13.5,
        "ts": valuation_now.isoformat(),
        "provider_ts": None,
        "received_at": valuation_now.isoformat(),
        "time_basis": "client_receive",
        "market_data_type": 1,
        "market_data_entitlement": "live",
    }
    underlying_quote = {
        **_gex_test_spot_observation(
            5000.0,
            observed_at=valuation_now,
        ),
        "status": "live",
        "is_stale": False,
        "is_delayed": False,
    }
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=valuation_now,
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4980,
                "right": "P",
                "gamma": 0.02,
                "delta": -0.38,
                "iv": 0.19,
                "time_to_expiry_years": 0.003,
                "reference_option_price": 18.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 2000,
                "abs_gex": 2000,
                "bid": 18.0,
                "ask": 19.0,
                "last": 18.5,
                "con_id": 4,
                "local_symbol": "ES P4980",
            },
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 5,
                "local_symbol": "ES P4970",
            },
        ],
    )

    high_target = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=5010,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=valuation_now,
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        exchange="CME",
        underlying_price=5000.0,
        underlying_quote=underlying_quote,
        option_quote_provider=lambda _payload: option_quote,
    )
    lower_target = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=valuation_now,
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        exchange="CME",
        underlying_price=5000.0,
        underlying_quote=underlying_quote,
        option_quote_provider=lambda _payload: option_quote,
    )

    assert high_target["ok"] is True
    assert high_target["fixed_contract"] is True
    assert high_target["strike"] == 4970.0
    assert high_target["iv_source"] == "market_implied"
    assert high_target["fair_price_status"] == "ok"
    assert lower_target["fair_price"] > high_target["fair_price"]


def test_option_target_fair_aligns_with_market_when_iv_is_stale(monkeypatch) -> None:
    valuation_now = datetime(2026, 6, 17, 15, 44, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 17, 15, 30, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-17T11:30:00-04:00",
            "spot": 5000.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260618",
                "strike": 4970.0,
                "right": "P",
                "gamma": 0.02,
                "delta": -0.41,
                "iv": 0.19,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": -1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 99,
                "local_symbol": "ES P4970",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=5000.0,
        target_ts=valuation_now,
        now=valuation_now,
        right="P",
        strike=4970.0,
        expiry="20260618",
        con_id=99,
        exchange="CME",
        underlying_price=5000.0,
        underlying_quote={
            **_gex_test_spot_observation(
                5000.0,
                observed_at=valuation_now,
            ),
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
        },
        option_quote_provider=lambda _payload: {
            "price": 13.5,
            "price_source": "bid_ask_mid",
            "bid": 13.2,
            "ask": 13.8,
            "last": 13.5,
            "ts": valuation_now.isoformat(),
            "provider_ts": None,
            "received_at": valuation_now.isoformat(),
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["ok"] is True
    assert result["iv_source"] == "market_implied"
    assert abs(result["fair_price"] - result["reference_option_price"]) <= 0.35


def test_option_call_fair_aligns_with_market_when_iv_is_stale(monkeypatch) -> None:
    valuation_now = datetime(2026, 6, 17, 15, 44, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_SPY,
        provider_symbol="SPY",
        captured_at=datetime(2026, 6, 17, 15, 30, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-17T11:30:00-04:00",
            "spot": 749.38,
            "risk_free_rate": 0.052,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260618",
                "strike": 750.0,
                "right": "C",
                "gamma": 0.0,
                "delta": 0.0,
                "iv": 0.19,
                "reference_option_price": 2.585,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 2.58,
                "ask": 2.59,
                "last": 2.585,
                "con_id": 99,
                "local_symbol": "SPY C750",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_SPY),
        target_price=749.38,
        target_ts=valuation_now,
        now=valuation_now,
        right="C",
        strike=750.0,
        expiry="20260618",
        con_id=99,
        exchange="SMART",
        underlying_price=749.38,
        underlying_quote={
            **_gex_test_spot_observation(
                749.38,
                observed_at=valuation_now,
            ),
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
        },
        option_quote_provider=lambda _payload: {
            "price": 2.585,
            "price_source": "bid_ask_mid",
            "bid": 2.58,
            "ask": 2.59,
            "last": 2.585,
            "ts": valuation_now.isoformat(),
            "provider_ts": None,
            "received_at": valuation_now.isoformat(),
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["ok"] is True
    assert result["iv_source"] == "market_implied"
    assert abs(result["fair_price"] - result["reference_option_price"]) <= 0.08
    assert result["delta"] is not None
    assert abs(result["delta"]) >= 0.2
    assert result["estimated_greeks"] is False


def test_option_target_delta_matches_fair_target_spot_after_live_quote(monkeypatch) -> None:
    valuation_now = datetime(2026, 6, 18, 15, 44, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 18, 15, 30, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-18T11:30:00-04:00",
            "spot": 7556.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260622",
                "strike": 7540.0,
                "right": "P",
                "gamma": 0.0,
                "delta": 0.0,
                "iv": 0.19,
                "reference_option_price": 17.4,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 17.3,
                "ask": 17.5,
                "last": 17.4,
                "con_id": 99,
                "local_symbol": "ES P7540",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=7540.0,
        target_ts=valuation_now,
        now=valuation_now,
        right="P",
        strike=7540.0,
        expiry="20260622",
        con_id=99,
        exchange="CME",
        underlying_price=7556.0,
        underlying_quote={
            **_gex_test_spot_observation(
                7556.0,
                observed_at=valuation_now,
            ),
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
        },
        option_quote_provider=lambda _payload: {
            "bid": 17.3,
            "ask": 17.5,
            "last": 17.4,
            "price": 17.4,
            "price_source": "bid_ask_mid",
            "ts": "2026-06-18T15:44:00+00:00",
            "provider_ts": None,
            "received_at": "2026-06-18T15:44:00+00:00",
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["ok"] is True
    assert result["iv_source"] == "market_implied"
    assert result["reference_option_price"] == 17.4
    assert result["fair_price"] > result["reference_option_price"]
    assert "current_delta" not in result
    assert "current_gamma" not in result
    assert abs(result["delta"]) < abs(result["target_delta"])
    assert result["target_delta"] < -0.2


def test_option_target_reprices_near_expiry_es_when_initial_iv_cap_is_too_low(monkeypatch) -> None:
    valuation_now = datetime(2026, 7, 7, 21, 0, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 7, 7, 20, 55, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-07-07T16:55:00-04:00",
            "spot": 7650.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260707",
                "expiry_at": "2026-07-07T21:30:00+00:00",
                "strike": 7600.0,
                "right": "P",
                "gamma": 0.0,
                "delta": 0.0,
                "iv": 0.19,
                "reference_option_price": 0.01,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 0.01,
                "ask": 0.05,
                "last": 0.01,
                "con_id": 7600,
                "local_symbol": "ES P7600",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=7650.0,
        target_ts=valuation_now,
        now=valuation_now,
        right="P",
        strike=7600.0,
        expiry="20260707",
        con_id=7600,
        exchange="CME",
        underlying_price=7650.0,
        underlying_quote={
            **_gex_test_spot_observation(
                7650.0,
                observed_at=valuation_now,
            ),
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
        },
        option_quote_provider=lambda _payload: {
            "bid": 15.3,
            "ask": 15.5,
            "last": 15.4,
            "price": 15.4,
            "price_source": "bid_ask_mid",
            "ts": "2026-07-07T21:00:00+00:00",
            "provider_ts": None,
            "received_at": "2026-07-07T21:00:00+00:00",
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["ok"] is True
    assert result["iv_source"] == "market_implied"
    assert result["fair_price_status"] == "ok"
    assert abs(result["fair_price"] - result["reference_option_price"]) <= 0.1
    assert result["iv"] > 0.76
    assert result["target_delta"] < -0.2


def test_option_target_price_uses_current_option_universe_not_future_target_as_quote_time(
    monkeypatch,
) -> None:
    target_time = datetime(2026, 6, 1, 14, 5, tzinfo=UTC)
    old_captured = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    latest_captured = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)

    def install_universe(captured_at: datetime, iv: float) -> None:
        contracts = [
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "gamma": 0.02,
                "delta": -0.38,
                "iv": iv,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": -1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 5,
                "local_symbol": "ES P4970",
                "trading_class": "ES",
                "exchange": "CME",
                "multiplier": 50.0,
            },
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "C",
                "gamma": 0.02,
                "delta": 0.38,
                "iv": iv,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 105,
                "local_symbol": "ES C4970",
                "trading_class": "ES",
                "exchange": "CME",
                "multiplier": 50.0,
            },
        ]
        _install_option_universe(
            monkeypatch,
            instrument=_ES,
            provider_symbol="ES",
            captured_at=captured_at,
            meta={
                "spot": 5000.0,
                "risk_free_rate": 0.05,
                "dividend_yield": 0.0,
            },
            contracts=contracts,
        )

    install_universe(old_captured, 0.30)
    install_universe(latest_captured, 0.10)

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=target_time,
        now=datetime(2026, 6, 1, 16, 0, tzinfo=UTC),
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        exchange="CME",
    )

    assert result["ok"] is True
    assert result["iv"] == 0.10
    assert result["iv_source"] == "theoretical_default"
    assert result["captured_at"] == latest_captured.isoformat()
    assert result["option_universe_source"] == "test_qualified_option_chain"
    assert "gex_snapshot_source" not in result


def test_option_target_price_does_not_read_gex_capture_lane_state(
    monkeypatch,
) -> None:
    live_at = datetime(2026, 6, 1, 14, 5, tzinfo=UTC)
    contracts = [
        {
            "expiry": "20260602",
            "strike": 4970,
            "right": "P",
            "gamma": 0.02,
            "delta": -0.38,
            "iv": 0.3,
            "reference_option_price": 13.5,
            "open_interest": 100,
            "volume": 20,
            "gex": -1000,
            "abs_gex": 1000,
            "bid": 13.2,
            "ask": 13.8,
            "last": 13.5,
            "con_id": 5,
            "local_symbol": "ES P4970",
            "trading_class": "ES",
            "exchange": "CME",
            "multiplier": 50.0,
        }
    ]
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=live_at,
        meta={
            "spot": 5000.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=contracts,
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=live_at + timedelta(minutes=1),
        now=live_at + timedelta(minutes=1),
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        exchange="CME",
        store=object(),
    )

    assert result["ok"] is True
    assert result["option_universe_source"] == "test_qualified_option_chain"
    assert result["pricing_basis"] == "theoretical_chain"
    assert result["subscription_state"] == "not_subscribed"
    assert "gex_snapshot_source" not in result


def test_option_target_price_does_not_read_gex_authority_state(monkeypatch) -> None:
    captured_at = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=captured_at,
        meta={
            "spot": 5000.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "gamma": 0.02,
                "delta": -0.38,
                "iv": 0.3,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": -1000,
                "abs_gex": 1000,
                "con_id": 5,
                "exchange": "CME",
                "multiplier": 50.0,
            }
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES, 100.0),
        target_price=4990,
        target_ts=captured_at + timedelta(minutes=1),
        now=captured_at + timedelta(minutes=1),
        right="P",
        store=object(),
    )

    assert result["ok"] is True
    assert result["pricing_basis"] == "theoretical_chain"
    assert result["subscription_state"] == "not_subscribed"
    assert "quality_code" not in result


def test_option_target_price_has_no_gex_freshness_gate(monkeypatch) -> None:
    captured_at = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=captured_at,
        meta={
            "spot": 5000.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": -1000,
                "abs_gex": 1000,
                "con_id": 5,
                "exchange": "CME",
            }
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=captured_at + timedelta(minutes=61),
        now=captured_at + timedelta(minutes=61),
        right="P",
    )

    assert result["ok"] is True
    assert result["pricing_basis"] == "theoretical_chain"
    assert "decision_authoritative" not in result


def test_option_target_price_merges_live_quote_for_fixed_contract(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 5,
                "exchange": "CME",
                "local_symbol": "ES P4970",
            },
        ],
    )
    seen = {}

    def live_provider(payload: dict[str, Any]) -> dict[str, Any]:
        seen.update(payload)
        return {
            "price": 21.5,
            "price_source": "bid_ask_mid",
            "bid": 21.25,
            "ask": 21.75,
            "last": 21.5,
            "ts": "2026-06-01T14:00:00+00:00",
            "provider_ts": None,
            "received_at": "2026-06-01T14:00:00+00:00",
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        }

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        local_symbol="ES P4970",
        exchange="CME",
        option_quote_provider=live_provider,
    )

    assert seen["con_id"] == 5
    assert seen["exchange"] == "CME"
    assert seen["sec_type"] == "FOP"
    assert result["ok"] is True
    assert result["fixed_contract"] is True
    assert result["strike"] == 4970.0
    assert result["live_bid"] == 21.25
    assert result["live_ask"] == 21.75
    assert result["live_mid"] == 21.5
    assert result["reference_option_price"] == 21.5
    assert result["live_quote_status"] == "ok"
    assert result["live_quote_source"] == "ibkr_option_quote"


def test_option_target_price_implies_fixed_contract_fair_when_cached_iv_missing(
    monkeypatch,
) -> None:
    valuation_now = datetime(2026, 7, 9, 11, 40, 26, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 7, 9, 11, 35, 41, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-07-09T07:35:41-04:00",
            "spot": 7539.875,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260710",
                "strike": 7490.0,
                "right": "P",
                "reference_option_price": 13.55,
                "open_interest": 622,
                "volume": 317,
                "gex": 0,
                "abs_gex": 0,
                "bid": 13.1,
                "ask": 13.4,
                "last": 15.0,
                "con_id": 891067893,
                "local_symbol": "EW2N6 P7490",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=7543.567245612045,
        target_ts=datetime(2026, 7, 9, 11, 45, tzinfo=UTC),
        now=valuation_now,
        right="P",
        strike=7490.0,
        expiry="20260710",
        con_id=891067893,
        local_symbol="EW2N6 P7490",
        exchange="CME",
        underlying_price=7539.875,
        underlying_quote={
            **_gex_test_spot_observation(
                7539.875,
                observed_at=valuation_now,
            ),
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
        },
        option_quote_provider=lambda _payload: {
            "price": 13.55,
            "price_source": "bid_ask_mid",
            "bid": 13.4,
            "ask": 13.7,
            "last": 15.0,
            "ts": "2026-07-09T11:40:26+00:00",
            "provider_ts": None,
            "received_at": "2026-07-09T11:40:26+00:00",
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["ok"] is True
    assert result["fixed_contract"] is True
    assert result["fixed_contract_quote_source"] == "qualified_contract_live_subscription"
    assert result.get("market_price_only") is not True
    assert result["fair_price_status"] == "ok"
    assert result["iv_source"] == "market_implied"
    assert result["fair_price"] is not None
    assert result["target_delta"] is not None
    assert result["reference_option_price"] == 13.55
    assert result["live_quote_status"] == "ok"


def test_option_target_price_matches_fixed_contract_by_con_id_first(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 5,
                "local_symbol": "ES P4970",
            },
            {
                "expiry": "20260602",
                "strike": 4960,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 11.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 900,
                "abs_gex": 900,
                "bid": 11.2,
                "ask": 11.8,
                "last": 11.5,
                "con_id": 6,
                "local_symbol": "ES P4960",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        right="P",
        con_id=5,
        local_symbol="BROKER FORMAT DRIFT",
    )
    mismatch = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        right="P",
        con_id=5,
        strike=4960,
        expiry="20260602",
        exchange="CME",
    )

    assert result["ok"] is True
    assert result["fixed_contract"] is True
    assert result["con_id"] == 5
    assert result["local_symbol"] == "ES P4970"
    assert result["strike"] == 4970.0
    assert mismatch["ok"] is False
    assert mismatch["code"] == "OPTION_TARGET_CONTRACT_FACT_MISMATCH"


def test_option_target_price_rejects_local_symbol_without_exact_con_id(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={"timestamp_ny": "2026-06-01T10:00:00-04:00", "spot": 5000},
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "con_id": 5,
                "local_symbol": "ES P4970",
                "gex": -1000,
                "abs_gex": 1000,
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        local_symbol="ES P4970",
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    )

    assert result["ok"] is False
    assert "exact provider conId" in result["message"]


def test_option_target_price_uses_live_quote_for_fixed_contract_missing_from_universe(
    monkeypatch,
) -> None:
    valuation_now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=valuation_now,
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4960,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 11.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 900,
                "abs_gex": 900,
                "bid": 11.2,
                "ask": 11.8,
                "last": 11.5,
                "con_id": 6,
                "local_symbol": "ES P4960",
            },
        ],
    )
    seen = {}

    def live_provider(payload: dict[str, Any]) -> dict[str, Any]:
        seen.update(payload)
        return {
            "price": 22.25,
            "price_source": "bid_ask_mid",
            "bid": 22.0,
            "ask": 22.5,
            "last": 22.25,
            "ts": "2026-06-01T14:00:00+00:00",
            "provider_ts": None,
            "received_at": "2026-06-01T14:00:00+00:00",
            "time_basis": "client_receive",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        }

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=valuation_now,
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        local_symbol="ES P4970",
        exchange="CME",
        underlying_price=5000.0,
        underlying_quote={
            **_gex_test_spot_observation(
                5000.0,
                observed_at=valuation_now,
            ),
            "status": "live",
            "is_stale": False,
            "is_delayed": False,
        },
        option_quote_provider=live_provider,
    )

    assert seen["con_id"] == 5
    assert result["ok"] is True
    assert result["fixed_contract"] is True
    assert result["fixed_contract_quote_source"] == "qualified_contract_live_subscription"
    assert result.get("market_price_only") is not True
    assert result["fair_price"] is not None
    assert result["max_limit_price"] is not None
    assert result["fair_price_status"] == "ok"
    assert result["iv_source"] == "market_implied"
    assert result["con_id"] == 5
    assert result["local_symbol"] == "ES P4970"
    assert result["live_bid"] == 22.0
    assert result["live_ask"] == 22.5
    assert result["reference_option_price"] == 22.25


def test_option_target_price_does_not_treat_zero_live_quote_as_current(monkeypatch) -> None:
    _install_option_universe(
        monkeypatch,
        instrument=_ES,
        provider_symbol="ES",
        captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        meta={
            "timestamp_ny": "2026-06-01T10:00:00-04:00",
            "spot": 5000,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        contracts=[
            {
                "expiry": "20260602",
                "strike": 4970,
                "right": "P",
                "time_to_expiry_years": 0.003,
                "reference_option_price": 13.5,
                "open_interest": 100,
                "volume": 20,
                "gex": 1000,
                "abs_gex": 1000,
                "bid": 13.2,
                "ask": 13.8,
                "last": 13.5,
                "con_id": 5,
                "local_symbol": "ES P4970",
            },
        ],
    )

    result = gex.option_target_price(
        **_option_identity_kwargs(_ES),
        target_price=4990,
        target_ts=datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        now=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        right="P",
        strike=4970,
        expiry="20260602",
        con_id=5,
        local_symbol="ES P4970",
        exchange="CME",
        option_quote_provider=lambda _payload: {
            "price": None,
            "price_source": "unavailable",
            "bid": 0.0,
            "ask": 0.0,
            "last": 0.0,
            "message": "no live option quote",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        },
    )

    assert result["ok"] is True
    assert result["fixed_contract"] is True
    assert result["live_quote_status"] == "empty"
    assert result["reference_option_price"] is None
    assert "current_option_price" not in result
    assert "bid" not in result
    assert "ask" not in result
    assert result["live_bid"] is None
    assert result["live_ask"] is None
    assert result["live_mid"] is None
