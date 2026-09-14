from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarProvenance,
)
from aef_terminal.engine.serialization import serialize_bar
from aef_terminal.storage.repos.bars import (
    BarsRepoMixin,
    _storage_bar_reject_reason,
)
from aef_terminal.storage.repos.futures import FuturesRepoMixin
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


class _LeaseNeutralRepo:
    def _require_canonical_writer_lease(self) -> None:
        return None

    def _acquire_canonical_writer_fence_on_cursor(self, _cur) -> None:
        return None


def _provider_bar(instrument: dict, *, symbol: str) -> Bar:
    route = route_instrument(instrument, expected_source="ibkr")
    return Bar(
        symbol=symbol,
        ts=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        timeframe="5m",
        source="arbitrary-advisory-text",
        closed=True,
        provenance=BarProvenance(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            request_type="historical",
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
            provider_contract_type=(
                "CONTFUT" if route.instrument.get("asset_class") == "future" else "STK"
            ),
            data_type=route.adapter.bar_data_type(route.instrument),
        ),
    )


def test_closed_bar_admission_uses_typed_route_not_source_text() -> None:
    route = route_instrument(
        ibkr_stock_payload("SPY"),
        expected_source="ibkr",
    )
    bar = _provider_bar(route.instrument, symbol="SPY")

    assert (
        _storage_bar_reject_reason(
            "ibkr",
            bar,
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            expected_provider_contract_id=route.adapter.session_contract_id(route.instrument),
            expected_data_type=route.adapter.bar_data_type(route.instrument),
        )
        is None
    )
    assert (
        _storage_bar_reject_reason(
            "ibkr",
            bar,
            instrument_id=route.instrument_id,
            expected_route_fingerprint="different-route",
        )
        == "route_fingerprint_mismatch"
    )
    assert (
        _storage_bar_reject_reason(
            "ibkr",
            Bar(
                symbol=bar.symbol,
                ts=bar.ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                timeframe=bar.timeframe,
                source=(
                    f"ibkr:historical:TRADES:contract=provider-contract:route={route.fingerprint}"
                ),
                closed=True,
            ),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
        )
        == "provider_provenance_missing"
    )


def test_canonical_admission_rejects_subminute_provider_bar_timestamp() -> None:
    route = route_instrument(
        ibkr_stock_payload("SPY"),
        expected_source="ibkr",
    )
    bar = replace(
        _provider_bar(route.instrument, symbol="SPY"),
        ts=datetime(2026, 7, 20, 12, 0, 30, tzinfo=UTC),
        timeframe="1m",
    )

    assert (
        _storage_bar_reject_reason(
            "ibkr",
            bar,
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            expected_provider_contract_id=route.adapter.session_contract_id(route.instrument),
            expected_data_type=route.adapter.bar_data_type(route.instrument),
        )
        == "provider_timestamp_not_minute_aligned"
    )


def test_canonical_admission_preserves_minute_precision_session_opening_bar() -> None:
    route = route_instrument(
        ibkr_stock_payload("SPY"),
        expected_source="ibkr",
    )
    opening_partial = replace(
        _provider_bar(route.instrument, symbol="SPY"),
        ts=datetime(2026, 7, 20, 13, 30, tzinfo=UTC),
        timeframe="60m",
    )

    assert (
        _storage_bar_reject_reason(
            "ibkr",
            opening_partial,
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            expected_provider_contract_id=route.adapter.session_contract_id(route.instrument),
            expected_data_type=route.adapter.bar_data_type(route.instrument),
        )
        is None
    )


def test_internal_bar_provenance_does_not_widen_chart_wire_payload() -> None:
    bar = _provider_bar(ibkr_stock_payload("SPY"), symbol="SPY")

    assert "provenance" not in serialize_bar(bar)


def test_bar_provenance_preserves_opaque_identity_tokens_exactly() -> None:
    provenance = BarProvenance(
        provider="ibkr",
        instrument_id=" instrument-id ",
        route_fingerprint=" route-fingerprint ",
        request_type="historical",
        provider_contract_id=" provider-contract ",
        provider_contract_type="STK",
        data_type="TRADES",
    )

    assert provenance.instrument_id == " instrument-id "
    assert provenance.route_fingerprint == " route-fingerprint "
    assert provenance.provider_contract_id == " provider-contract "
    assert provenance.provider_contract_type == "STK"


def test_aggregation_provenance_rejects_malformed_source_timeframe() -> None:
    with pytest.raises(
        ValueError,
        match="BAR_PROVENANCE_AGGREGATION_SOURCE_TIMEFRAME_INVALID",
    ):
        BarProvenance(
            provider="demo",
            instrument_id="demo|contract|MIX",
            route_fingerprint="demo-route",
            request_type=BarProviderRequest.DETERMINISTIC_AGGREGATION,
            provider_contract_id="MXU6",
            provider_contract_type="FUT",
            data_type="TRADES",
            source_timeframe="garbage",
        )


def test_generic_storage_rejects_bar_from_another_instrument_before_sql() -> None:
    class Repo(BarsRepoMixin, _LeaseNeutralRepo):
        def _connect(self):
            raise AssertionError("route-mismatched bar must not reach SQL")

    target = ibkr_stock_payload("QQQ", con_id=320227571)
    with pytest.raises(ValueError, match="instrument_id_mismatch"):
        Repo().write_bars(
            [
                _provider_bar(
                    ibkr_stock_payload("SPY", con_id=756733),
                    symbol="SPY",
                )
            ],
            "ibkr",
            instrument=target,
            revision_sequence=1,
        )


def test_all_canonical_bar_writers_reject_virtual_three_minute_rows_before_sql() -> None:
    ordinary_instrument = ibkr_stock_payload("SPY")
    future_instrument = ibkr_future_payload("ES")
    ordinary_bar = replace(
        _provider_bar(ordinary_instrument, symbol="SPY"),
        timeframe="3m",
    )
    future_bar = replace(
        _provider_bar(future_instrument, symbol="ES"),
        timeframe="3m",
    )

    class OrdinaryRepo(BarsRepoMixin, _LeaseNeutralRepo):
        def _connect(self):
            raise AssertionError("virtual 3m must fail before ordinary SQL")

    class FuturesRepo(FuturesRepoMixin, _LeaseNeutralRepo):
        def _psycopg(self) -> None:
            return None

        def _connect(self):
            raise AssertionError("virtual 3m must fail before futures SQL")

    error = "CANONICAL_BAR_STORAGE_TIMEFRAME_UNSUPPORTED timeframe=3m"
    with pytest.raises(ValueError, match=error):
        OrdinaryRepo().write_bars(
            [ordinary_bar],
            "ibkr",
            instrument=ordinary_instrument,
            revision_sequence=1,
        )
    with pytest.raises(ValueError, match=error):
        FuturesRepo().write_futures_contract_bars(
            [future_bar],
            provider="ibkr",
            instrument_id="ibkr|contract|649180671",
            route_fingerprint="ibkr|contract|649180671",
            contract_key="649180671",
            provider_contract_id="649180671",
            provider_contract_type="FUT",
            data_type="TRADES",
            revision_sequence=2,
        )
    with pytest.raises(ValueError, match=error):
        FuturesRepo().write_futures_canonical_bars(
            [future_bar],
            provider="ibkr",
            instrument_id="ibkr|future_root|ES|CME|USD|ES",
            route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:649180671",
            series_type="provider_native",
            roll_policy="provider_managed",
            metadata={
                "provider_contract_id": "649180671",
                "provider_contract_type": "CONTFUT",
                "data_type": "TRADES",
            },
            revision_sequence=3,
        )


@pytest.mark.parametrize("legacy_alias", ("1h", "60"))
def test_all_canonical_bar_readers_reject_legacy_hour_aliases_before_sql(
    legacy_alias: str,
) -> None:
    class OrdinaryRepo(BarsRepoMixin, _LeaseNeutralRepo):
        def _connect(self):
            raise AssertionError("legacy timeframe aliases must fail before ordinary SQL")

    class FuturesRepo(FuturesRepoMixin, _LeaseNeutralRepo):
        def _connect(self):
            raise AssertionError("legacy timeframe aliases must fail before futures SQL")

    error = rf"CANONICAL_BAR_STORAGE_TIMEFRAME_UNSUPPORTED timeframe={legacy_alias}"
    ordinary_instrument = ibkr_stock_payload("SPY")
    with pytest.raises(ValueError, match=error):
        OrdinaryRepo().read_bars(
            legacy_alias,
            "ibkr",
            instrument=ordinary_instrument,
        )
    with pytest.raises(ValueError, match=error):
        OrdinaryRepo().read_bars_multi(
            (legacy_alias,),
            ("ibkr",),
            instrument=ordinary_instrument,
        )

    futures_repo = FuturesRepo()
    with pytest.raises(ValueError, match=error):
        futures_repo.read_futures_contract_bars_multi(
            provider="ibkr",
            instrument_id="ibkr|contract|649180671",
            route_fingerprint="ibkr|contract|649180671",
            symbol="ES",
            contract_key="649180671",
            timeframes=(legacy_alias,),
        )
    with pytest.raises(ValueError, match=error):
        futures_repo.read_futures_canonical_bars_multi(
            provider="ibkr",
            instrument_id="ibkr|future_root|ES|CME|USD|ES",
            route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:649180671",
            symbol="ES",
            timeframes=(legacy_alias,),
            series_type="provider_native",
            roll_policy="provider_managed",
        )


@pytest.mark.parametrize(
    ("request_type", "provider_contract_id", "data_type", "reason"),
    [
        (
            BarProviderRequest.CANONICAL_STORAGE,
            "",
            "canonical_ohlcv",
            "provider_request_type_mismatch",
        ),
        (
            BarProviderRequest.HISTORICAL,
            "wrong-contract",
            "TRADES",
            "provider_contract_id_mismatch",
        ),
        (
            BarProviderRequest.HISTORICAL,
            "__expected__",
            "MIDPOINT",
            "provider_data_type_mismatch",
        ),
    ],
)
def test_generic_storage_rejects_wrong_provider_lane_before_sql(
    request_type: BarProviderRequest,
    provider_contract_id: str,
    data_type: str,
    reason: str,
) -> None:
    class Repo(BarsRepoMixin, _LeaseNeutralRepo):
        def _connect(self):
            raise AssertionError("wrong provider lane must not reach SQL")

    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument, expected_source="ibkr")
    exact_provider_contract_id = route.adapter.session_contract_id(route.instrument)
    bar = _provider_bar(instrument, symbol="SPY")
    wrong_lane = Bar(
        symbol=bar.symbol,
        ts=bar.ts,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        timeframe=bar.timeframe,
        source=bar.source,
        closed=bar.closed,
        provenance=BarProvenance(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            request_type=request_type,
            provider_contract_id=(
                exact_provider_contract_id
                if provider_contract_id == "__expected__"
                else provider_contract_id
            ),
            provider_contract_type="STK",
            data_type=data_type,
        ),
    )

    with pytest.raises(ValueError, match=reason):
        Repo().write_bars(
            [wrong_lane],
            "ibkr",
            instrument=instrument,
            revision_sequence=1,
        )


def test_storage_rejects_malformed_aggregation_timeframe_before_sql() -> None:
    class Repo(BarsRepoMixin, _LeaseNeutralRepo):
        def _connect(self):
            raise AssertionError("malformed aggregation must not reach SQL")

    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument, expected_source="ibkr")
    bar = _provider_bar(instrument, symbol="SPY")
    malformed = Bar(
        symbol=bar.symbol,
        ts=bar.ts,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        timeframe="garbage",
        source=bar.source,
        closed=True,
        provenance=BarProvenance(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            request_type=BarProviderRequest.DETERMINISTIC_AGGREGATION,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
            provider_contract_type="STK",
            data_type=route.adapter.bar_data_type(route.instrument),
            source_timeframe="1m",
        ),
    )

    with pytest.raises(
        ValueError,
        match="CANONICAL_BAR_STORAGE_TIMEFRAME_UNSUPPORTED timeframe=garbage",
    ):
        Repo().write_bars(
            [malformed],
            "ibkr",
            instrument=instrument,
            revision_sequence=1,
        )


def test_futures_storage_rejects_bar_from_another_route_before_sql() -> None:
    class Repo(FuturesRepoMixin, _LeaseNeutralRepo):
        def _psycopg(self) -> None:
            return None

        def _connect(self):
            raise AssertionError("route-mismatched futures bar must not reach SQL")

    source_route = route_instrument(
        ibkr_future_payload("ES"),
        expected_source="ibkr",
    )
    with pytest.raises(ValueError, match="route_fingerprint_mismatch"):
        Repo().write_futures_canonical_bars(
            [_provider_bar(source_route.instrument, symbol="ES")],
            provider="ibkr",
            instrument_id=source_route.instrument_id,
            route_fingerprint="different-route",
            series_type="provider_native",
            roll_policy="provider_managed",
            metadata={
                "provider_contract_id": source_route.adapter.session_contract_id(
                    source_route.instrument
                ),
                "provider_contract_type": "CONTFUT",
                "data_type": source_route.adapter.bar_data_type(source_route.instrument),
            },
            revision_sequence=2,
        )


def test_futures_storage_rejects_contract_type_mismatch_before_sql() -> None:
    class Repo(FuturesRepoMixin, _LeaseNeutralRepo):
        def _psycopg(self) -> None:
            return None

        def _connect(self):
            raise AssertionError("contract-type-mismatched futures bar must not reach SQL")

    route = route_instrument(
        ibkr_future_payload("ES"),
        expected_source="ibkr",
    )
    bar = _provider_bar(route.instrument, symbol="ES")
    assert bar.provenance is not None
    wrong_type = Bar(
        symbol=bar.symbol,
        ts=bar.ts,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        timeframe=bar.timeframe,
        source=bar.source,
        closed=bar.closed,
        provenance=BarProvenance(
            provider=bar.provenance.provider,
            instrument_id=bar.provenance.instrument_id,
            route_fingerprint=bar.provenance.route_fingerprint,
            request_type=bar.provenance.request_type,
            provider_contract_id=bar.provenance.provider_contract_id,
            provider_contract_type="FUT",
            data_type=bar.provenance.data_type,
        ),
    )

    with pytest.raises(
        ValueError,
        match="provider_contract_type_mismatch",
    ):
        Repo().write_futures_canonical_bars(
            [wrong_type],
            provider="ibkr",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            series_type="provider_native",
            roll_policy="provider_managed",
            metadata={
                "provider_contract_id": (route.adapter.session_contract_id(route.instrument)),
                "provider_contract_type": "CONTFUT",
                "data_type": route.adapter.bar_data_type(route.instrument),
            },
            revision_sequence=1,
        )
