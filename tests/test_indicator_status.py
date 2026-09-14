import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest

from aef_terminal.config import AppConfig
from aef_terminal.domain import Bar
from aef_terminal.engine.analyze import analyze_bars as _analyze_bars
from aef_terminal.engine.analyze import bars as analyze_bars_module
from aef_terminal.engine.analyze.features import _json_safe
from aef_terminal.data.provider_sessions import (
    ProviderBarSlotMap,
    provider_schedule_open_state,
)
from aef_terminal.data.providers import get_provider
from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.engine.data_quality import serialize_data_quality_report, snapshot_freshness
from aef_terminal.engine.data_quality import data_quality_report as _data_quality_report
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from tests.provider_payloads import (
    coinbase_btc_payload,
    ibkr_future_payload,
    ibkr_stock_payload,
    instrument_with_bar_sessions,
)
from aef_terminal.engine.serialization import serialize_bars
from aef_terminal.engine.snapshot import builder as snapshot_builder_module
from aef_terminal.engine.snapshot.empty import empty_market_snapshot
from aef_terminal.engine.snapshot.range import require_signal_range
from aef_terminal.engine.analysis_db import provider_backed_bar_slots


def data_quality_report(bars, interval, *args, **kwargs):
    assert isinstance(kwargs.get("instrument"), dict), (
        "test must supply exact provider-qualified instrument"
    )
    return _data_quality_report(bars, interval, *args, **kwargs)


def analyze_bars(bars, *args, **kwargs):
    assert isinstance(kwargs.get("instrument"), dict), (
        "test must supply exact provider-qualified instrument"
    )
    kwargs["instrument"] = instrument_with_bar_sessions(kwargs["instrument"], bars)
    return _analyze_bars(bars, *args, **kwargs)


analysis_module = SimpleNamespace(
    AppConfig=AppConfig,
    async_build_market_snapshot=snapshot_builder_module.async_build_market_snapshot_from_db,
    _json_safe=_json_safe,
)


class FreshIbkrGapStore:
    def initialize(self):
        return None

    def read_trading_hours(self, *, instrument: dict):
        assert instrument["provider"] == "ibkr"
        return {"age_seconds": 60.0}

    def read_bars(self, **_kwargs):
        return []


class OpenIbkrSessionStore:
    def initialize(self):
        return None

    def read_trading_hours(self, *, instrument: dict):
        assert instrument["provider"] == "ibkr"
        return {
            "timezone": "America/New_York",
            "payload": {
                "provider_contract_id": get_provider("ibkr").session_contract_id(instrument)
            },
        }

    def read_trading_session_open_state(
        self,
        ts: datetime,
        session_type: str,
        *,
        instrument: dict,
    ):
        assert instrument["provider"] == "ibkr"
        return True

    def read_trading_session_intervals(
        self,
        *,
        instrument: dict,
        session_type: str,
        start_ts: datetime,
        end_ts: datetime,
        limit: int | None = None,
    ):
        _ = limit
        assert instrument["provider"] == "ibkr"
        return [
            {
                "opens_at": (start_ts - timedelta(days=1)).isoformat(),
                "closes_at": (end_ts + timedelta(days=1)).isoformat(),
                "status": "open",
            }
        ]


def make_bar(index: int, price: float, *, closed: bool = True) -> Bar:
    ts = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    close = price + (0.4 if index % 3 == 0 else -0.15)
    return Bar(
        symbol="ES",
        ts=ts,
        open=price,
        high=max(price, close) + 0.75,
        low=min(price, close) - 0.55,
        close=close,
        volume=1200 + index * 12,
        timeframe="5m",
        source="test",
        closed=closed,
    )


def make_session_bar(ts: datetime, index: int, price: float) -> Bar:
    close = price + (0.25 if index % 2 == 0 else -0.2)
    return Bar(
        symbol="ES",
        ts=ts,
        open=price,
        high=max(price, close) + 0.5,
        low=min(price, close) - 0.5,
        close=close,
        volume=1500 + index * 10,
        timeframe="5m",
        source="test",
        closed=True,
    )


def test_provider_backed_bar_slots_use_store_mapping_when_available() -> None:
    bars = [make_bar(index, 5000 + index) for index in range(3)]

    class Store:
        def read_bar_slots(self, **kwargs):
            assert kwargs["provider"] == "ibkr"
            assert "symbol" not in kwargs
            assert kwargs["timeframe"] == "5m"
            assert kwargs["step"] == 5
            return ProviderBarSlotMap(
                {
                    bars[0].ts: 1000,
                    bars[1].ts: 1005,
                    bars[2].ts: 1010,
                },
                schedule_state="verified",
            )

    with pytest.raises(
        RuntimeError,
        match="FULL_SNAPSHOT_CONTEXT_REQUIRED_FOR_PROVIDER_BAR_SLOTS",
    ):
        provider_backed_bar_slots(Store(), bars, instrument=ibkr_future_payload("ES"))


def test_provider_backed_bar_slots_requires_storage_mapping() -> None:
    bars = [make_bar(index, 5000 + index) for index in range(3)]
    assert provider_backed_bar_slots(None, bars, instrument=ibkr_future_payload("ES")) is None


def test_serialize_bars_emits_only_real_bars_across_timestamp_omissions() -> None:
    first = make_bar(0, 5000)
    second = Bar(
        symbol="ES",
        ts=first.ts + timedelta(minutes=15),
        open=5002,
        high=5003,
        low=5001,
        close=5002.5,
        volume=1500,
        timeframe="5m",
        source="test",
        closed=True,
    )
    serialized = serialize_bars([first, second])

    assert len(serialized) == 2
    assert [item["ts"] for item in serialized] == [
        first.ts.isoformat(),
        second.ts.isoformat(),
    ]
    assert all(item.get("missing") is not True for item in serialized)


def test_serialize_bars_requires_one_exact_typed_slot_axis() -> None:
    bars = [make_bar(index, 5000 + index) for index in range(2)]

    with pytest.raises(TypeError, match="ProviderBarSlotSequence"):
        serialize_bars([], [])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ProviderBarSlotSequence"):
        serialize_bars(bars, [10, 15])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="align exactly"):
        serialize_bars(
            bars,
            ProviderBarSlotSequence([10], schedule_state="verified"),
        )


def test_provider_bar_slot_sequence_rejects_ambiguous_or_unordered_values() -> None:
    with pytest.raises(ValueError, match="schedule_state"):
        ProviderBarSlotSequence([10], schedule_state="stale")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integers"):
        ProviderBarSlotSequence([True], schedule_state="verified")
    with pytest.raises(ValueError, match="strictly increasing"):
        ProviderBarSlotSequence([10, 10], schedule_state="verified")


def test_data_quality_does_not_reconstruct_historical_slots() -> None:
    start = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
    bars = [
        Bar("BTC-USD", start, 100, 101, 99, 100.5, 10, "5m", "coinbase:BTC-USD", closed=True),
        Bar(
            "BTC-USD",
            start + timedelta(minutes=10),
            100.5,
            102,
            100,
            101.5,
            11,
            "5m",
            "coinbase:BTC-USD",
            closed=True,
        ),
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=start + timedelta(minutes=15),
        instrument=coinbase_btc_payload(),
    )

    assert quality["status"] == "ok"
    assert quality["signals_ok"] is True
    assert "gaps" not in quality


def test_data_quality_transport_rejects_untyped_or_non_finite_values() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        serialize_data_quality_report({"stale_minutes": float("nan")})
    with pytest.raises(TypeError, match="keys must be non-empty strings"):
        serialize_data_quality_report({1: "invalid"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="JSON-safe typed values"):
        serialize_data_quality_report({"opaque": object()})


def test_analyze_bars_attaches_runtime_status_to_each_indicator() -> None:
    bars = [make_bar(index, 5000 + index * 0.3) for index in range(90)]

    snapshot = analyze_bars(
        bars,
        display_symbol="ES",
        instrument=ibkr_future_payload("ES"),
        indicator_params={
            "breakout_accumulation": {"enabled": True},
            "impulse_fib": {"enabled": True},
            "market_spotlight": {"enabled": True},
            "linda_volume": {"enabled": True},
            "smc_channels": {"enabled": True},
            "w5_structure": {"enabled": True},
            "wolfe_structure": {"enabled": True},
        },
    )
    indicators = snapshot["indicators"]

    expected = {
        "breakout_accumulation",
        "market_spotlight",
        "linda_volume",
        "impulse_fib",
        "smc_channels",
        "w5_structure",
        "wolfe_structure",
    }
    assert expected <= set(indicators)
    assert "absorption_trap" not in indicators
    for key in expected:
        status = indicators[key]["status"]
        assert status["id"] == key
        assert status["label"]
        assert status["calculates"]
        assert status["bar_count"] > 0
        assert status["calculated_at"]
        assert status["state_code"] in {
            "no_signal",
            "signal",
            "blocked_context",
            "degraded_context",
            "blocked_signal",
            "live_preview_signal",
            "stale",
            "obsolete",
            "error",
        }
        assert status["trigger_event"]["code"]
        assert "state" not in status
        assert "message" not in status

    assert "vsa_volume" not in indicators
    vsa_volume = snapshot["vsa_volume"]
    assert vsa_volume["status"]["state"] == "degraded"
    assert vsa_volume["status"]["reason_code"] == "provider_slot_axis_unavailable"
    assert vsa_volume["status"]["mode"] == "confirmed"
    assert vsa_volume["status"]["bar_count"] == len(bars)
    assert vsa_volume["status"]["analysis_ts"] == bars[-1].ts.isoformat()
    assert len(vsa_volume["series"]) == len(bars)


def test_market_closed_blocks_entry_after_indicators_calculate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = [make_bar(index, 5000 + index * 0.3) for index in range(90)]
    monkeypatch.setattr(
        analyze_bars_module,
        "data_quality_report",
        lambda *_args, **_kwargs: {
            "status": "market_closed",
            "signals_ok": False,
            "market_closed": True,
            "warning": "MARKET CLOSED",
        },
    )

    snapshot = analyze_bars(
        bars,
        display_symbol="ES",
        instrument=ibkr_future_payload("ES"),
        indicator_params={"impulse_fib": {"enabled": True}},
    )

    assert "impulse_fib" in snapshot["indicators"]
    assert snapshot["indicators"]["impulse_fib"]["status"]["calculates"]
    assert snapshot["decision"]["action"] == "BLOCK"


def test_json_safe_converts_numpy_scalars() -> None:
    np = __import__("numpy")
    payload = {
        "flag": np.bool_(True),
        "nested": [{"count": np.int64(3), "value": np.float64(1.25)}],
    }

    safe = analysis_module._json_safe(payload)

    assert safe == {"flag": True, "nested": [{"count": 3, "value": 1.25}]}
    json.dumps(safe)


def test_json_safe_rejects_naive_timestamps() -> None:
    aware = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    assert analysis_module._json_safe(aware) == aware.isoformat()
    with pytest.raises(ValueError, match="analysis JSON timestamp must be timezone-aware"):
        analysis_module._json_safe(datetime(2026, 8, 7, 12, 0))


def test_json_safe_native_leaves_preserve_values_and_container_isolation() -> None:
    leaves = [None, True, False, 0, 42, -0.5, "signal"]
    original = {1: tuple(leaves), "rows": [{"value": 123.5}]}
    safe = analysis_module._json_safe(original)
    assert safe == {"1": leaves, "rows": [{"value": 123.5}]}
    assert all(actual is expected for actual, expected in zip(safe["1"], leaves, strict=True))
    assert safe["rows"] is not original["rows"]
    assert safe["rows"][0] is not original["rows"][0]


def test_snapshot_assembly_can_defer_json_normalization() -> None:
    timestamp = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    raw = {"bars": [], "captured_at": timestamp}

    deferred = analyze_bars_module._finalize_analysis_snapshot(
        raw,
        serialize_output=False,
    )
    finalized = analyze_bars_module._finalize_analysis_snapshot(
        {"bars": [], "captured_at": timestamp},
        serialize_output=True,
    )

    assert deferred is raw
    assert deferred["captured_at"] is timestamp
    assert finalized["captured_at"] == timestamp.isoformat()


def test_empty_market_snapshot_reports_indicator_errors() -> None:
    snapshot = empty_market_snapshot(ibkr_future_payload("ES"), "5m", "No history")

    for indicator in snapshot["indicators"].values():
        status = indicator["status"]
        assert status["state_code"] == "error"
        assert status["bar_count"] == 0
        assert status["trigger_event"] == {"code": "indicator_error"}
        assert "No history" in status["last_error"]


def test_signal_range_contract_defaults_only_when_absent() -> None:
    assert require_signal_range(None) == "2d"
    for value in ("1d", "2d", "3d", "7d"):
        assert require_signal_range(value) == value

    for invalid in ("", "5d", "14d", "off", " 2d ", 2, False):
        with pytest.raises(ValueError, match="SIGNAL_RANGE_INVALID"):
            require_signal_range(invalid)


def test_snapshot_uses_short_signal_history_without_chart_range_substitution(
    monkeypatch,
) -> None:
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    chart_bars = [
        make_session_bar(now - timedelta(minutes=5 * (39 - index)), index, 100.0)
        for index in range(40)
    ]
    signal_bars = [
        make_session_bar(now - timedelta(days=7) + timedelta(minutes=5 * index), index, 200.0)
        for index in range(12)
    ]
    loads: list[str] = []
    analyzed: dict[str, object] = {}

    def fake_load(_route, _interval, range_, _timeout, **_kwargs):
        loads.append(range_)
        return (chart_bars if range_ == "1d" else signal_bars), ""

    def fake_analyze(_instrument, _symbol, _interval, range_, bars, *_args, **kwargs):
        analyzed.update(range=range_, bars=list(bars), signal_range=kwargs["signal_range"])
        return {"meta": {}}

    monkeypatch.setattr(snapshot_builder_module, "load_provider_bars", fake_load)
    monkeypatch.setattr(snapshot_builder_module, "analyze_market_bars_for_snapshot", fake_analyze)

    snapshot_builder_module.build_market_snapshot_from_db(
        source="ibkr",
        interval="5m",
        range_="1d",
        signal_range_="7d",
        instrument=ibkr_future_payload("ES"),
    )

    assert loads == ["1d", "7d"]
    assert analyzed == {
        "range": "7d",
        "bars": signal_bars,
        "signal_range": "7d",
    }


def test_analysis_snapshot_builder_can_omit_chart_projection(monkeypatch) -> None:
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    bars = [
        make_session_bar(now - timedelta(minutes=5 * (39 - index)), index, 100.0)
        for index in range(40)
    ]
    analyzed: dict[str, object] = {}

    monkeypatch.setattr(
        snapshot_builder_module,
        "load_provider_bars",
        lambda *_args, **_kwargs: (bars, ""),
    )
    monkeypatch.setattr(snapshot_builder_module, "_bars_cover_range", lambda *_args: True)

    def fake_analyze(*_args, **kwargs):
        analyzed["chart_bars"] = kwargs["chart_bars"]
        return {"meta": {}}

    monkeypatch.setattr(
        snapshot_builder_module,
        "analyze_market_bars_for_snapshot",
        fake_analyze,
    )

    snapshot_builder_module.build_market_snapshot_from_db(
        source="ibkr",
        interval="5m",
        range_="1d",
        signal_range_="1d",
        include_chart_projection=False,
        instrument=ibkr_future_payload("ES"),
    )

    assert analyzed == {"chart_bars": ()}


def test_data_quality_flags_stale_es_bars() -> None:
    bars = [make_bar(index, 5000 + index * 0.3) for index in range(20)]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc),
        instrument=ibkr_future_payload("ES"),
    )

    assert quality["status"] == "stale+session_unknown"
    assert quality["signals_ok"] is False
    assert "LIVE STALE" in quality["warning"]
    assert quality["stale_minutes"] > quality["stale_threshold_minutes"]
    assert quality["latest_ts"] == bars[-1].ts
    assert quality["latest_ts"].tzinfo is not None
    assert quality["latest_ts"].utcoffset() is not None


def test_snapshot_freshness_reports_bar_age() -> None:
    bars = [make_bar(index, 5000 + index * 0.3) for index in range(20)]
    now = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    freshness = snapshot_freshness(
        timeframe="5m",
        source="ibkr:db-cache",
        latest_bar_ts=bars[-1].ts,
        updated_at=now,
    )

    assert freshness["status"] == "stale"
    assert freshness["bar_age_seconds"] is not None
    assert freshness["bar_age_seconds"] > 0
    assert freshness["latest_bar_ts"]


def test_generic_future_profile_uses_future_stale_threshold() -> None:
    now = datetime(2026, 8, 13, 7, 30, tzinfo=timezone.utc)
    instrument = ibkr_future_payload("MXU6")

    freshness = snapshot_freshness(
        timeframe="5m",
        source="tinvest:exchange",
        latest_bar_ts=now - timedelta(minutes=21),
        updated_at=now,
        instrument_profile=resolve_instrument_profile(instrument),
    )

    assert freshness["stale_threshold_seconds"] == 20 * 60
    assert freshness["status"] == "stale"


def test_data_quality_reports_provider_confirmed_first_bar_session_warmup() -> None:
    session_open = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
    session_close = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)

    class Store(OpenIbkrSessionStore):
        def read_trading_session_intervals(
            self,
            *,
            instrument: dict,
            session_type: str,
            start_ts: datetime,
            end_ts: datetime,
        ):
            assert instrument["provider"] == "ibkr"
            assert session_type == "trading"
            assert session_open <= start_ts < end_ts <= session_close
            return [
                {
                    "opens_at": session_open.isoformat(),
                    "closes_at": session_close.isoformat(),
                    "status": "open",
                }
            ]

    bars = [
        Bar(
            "SPY",
            datetime(2026, 7, 16, 18, 35, tzinfo=timezone.utc),
            750.68,
            750.68,
            750.35,
            750.36,
            231580,
            "5m",
            "ibkr:db-cache",
            closed=True,
        )
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 7, 17, 8, 3, tzinfo=timezone.utc),
        store=Store(),
        instrument=ibkr_stock_payload("SPY"),
    )

    assert quality["status"] == "session_warmup"
    assert quality["signals_ok"] is False
    assert quality["session_warmup"] is True
    assert quality["session_open_ts"] == session_open
    assert quality["first_confirmed_bar_due_ts"] == session_open + timedelta(minutes=5)
    assert "SESSION WARMUP" in quality["warning"]
    assert "LIVE STALE" not in quality["warning"]


def test_data_quality_session_warmup_expires_when_first_bar_is_due() -> None:
    session_open = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
    session_close = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)

    class Store(OpenIbkrSessionStore):
        def read_trading_session_intervals(self, *_args, **_kwargs):
            return [
                {
                    "opens_at": session_open.isoformat(),
                    "closes_at": session_close.isoformat(),
                    "status": "open",
                }
            ]

    bars = [
        Bar(
            "SPY",
            datetime(2026, 7, 16, 18, 35, tzinfo=timezone.utc),
            750.68,
            750.68,
            750.35,
            750.36,
            231580,
            "5m",
            "ibkr:db-cache",
            closed=True,
        )
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 7, 17, 8, 6, tzinfo=timezone.utc),
        store=Store(),
        instrument=ibkr_stock_payload("SPY"),
    )

    assert quality["status"] == "stale"
    assert quality["signals_ok"] is False
    assert quality["session_warmup"] is False
    assert "LIVE STALE" in quality["warning"]


def test_data_quality_index_session_warmup_uses_history_liquid_scope() -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    sessions_module._TRADING_HOURS_CACHE.clear()
    session_open = datetime(2026, 7, 17, 13, 30, tzinfo=timezone.utc)
    session_close = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)
    observed_session_types: list[str] = []

    class Store:
        @staticmethod
        def initialize():
            return None

        @staticmethod
        def read_trading_hours(*, instrument: dict):
            return {
                "timezone": "America/New_York",
                "payload": {
                    "provider_contract_id": get_provider("ibkr").session_contract_id(instrument)
                },
            }

        @staticmethod
        def read_trading_session_open_state(
            _ts: datetime,
            session_type: str,
            *,
            instrument: dict,
        ):
            observed_session_types.append(session_type)
            return True

        @staticmethod
        def read_trading_session_intervals(
            *,
            instrument: dict,
            session_type: str,
            start_ts: datetime,
            end_ts: datetime,
        ):
            observed_session_types.append(session_type)
            assert session_open <= start_ts < end_ts <= session_close
            return [
                {
                    "opens_at": session_open,
                    "closes_at": session_close,
                    "status": "open",
                }
            ]

    bars = [
        Bar(
            "SPX",
            datetime(2026, 7, 16, 19, 0, tzinfo=timezone.utc),
            6800.0,
            6801.0,
            6799.0,
            6800.5,
            100,
            "60m",
            "ibkr:db-cache",
            closed=True,
        )
    ]
    instrument = ibkr_stock_payload(
        "SPX",
        con_id=416904,
        asset_class="index",
        sec_type="IND",
    )

    quality = data_quality_report(
        bars,
        "60m",
        now_utc=datetime(2026, 7, 17, 13, 50, tzinfo=timezone.utc),
        store=Store(),
        instrument=instrument,
    )

    assert quality["status"] == "session_warmup"
    assert quality["session_open_ts"] == session_open
    assert quality["first_confirmed_bar_due_ts"] == datetime(
        2026,
        7,
        17,
        14,
        0,
        tzinfo=timezone.utc,
    )
    assert observed_session_types == ["liquid", "liquid"]
    sessions_module._TRADING_HOURS_CACHE.clear()


def test_expected_market_closed_notice_uses_history_liquid_scope_for_ibkr_index() -> None:
    from aef_terminal.data import provider_sessions as sessions_module
    from aef_terminal.data.provider_sessions import expected_market_closed_notice

    sessions_module._TRADING_HOURS_CACHE.clear()
    observed_session_types: list[str] = []
    reopen = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)

    class Store:
        @staticmethod
        def initialize():
            return None

        @staticmethod
        def read_trading_hours(*, instrument: dict):
            return {
                "payload": {
                    "provider_contract_id": get_provider("ibkr").session_contract_id(instrument)
                }
            }

        @staticmethod
        def read_trading_session_open_state(
            _ts: datetime,
            session_type: str,
            *,
            instrument: dict,
        ):
            observed_session_types.append(session_type)
            return False

        @staticmethod
        def read_next_trading_session_open(
            _ts: datetime,
            session_type: str,
            *,
            instrument: dict,
        ):
            observed_session_types.append(session_type)
            return reopen

    instrument = ibkr_stock_payload(
        "SPX",
        con_id=416904,
        asset_class="index",
        sec_type="IND",
    )
    notice = expected_market_closed_notice(
        datetime(2026, 7, 17, 19, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        "60m",
        store=Store(),
        instrument=instrument,
    )

    assert notice is not None
    assert notice["session"] == "market_closed"
    assert notice["reopen_ts"] == reopen
    assert notice["reopen_ts"].tzinfo is not None
    assert notice["reopen_ts"].utcoffset() is not None
    assert "latest 60m bar started at 2026-07-17T19:00:00+00:00" in notice["warning"]
    assert "bar ended at" not in notice["warning"]
    assert observed_session_types == ["liquid", "liquid"]
    sessions_module._TRADING_HOURS_CACHE.clear()


def test_data_quality_accepts_provider_confirmed_flat_zero_volume_stock_bar() -> None:
    bars = [
        Bar(
            "SPY",
            datetime(2026, 6, 12, 16, 0, tzinfo=timezone.utc),
            740.0,
            741.0,
            739.5,
            740.5,
            1000,
            "5m",
            "ibkr",
        ),
        Bar(
            "SPY",
            datetime(2026, 6, 12, 16, 5, tzinfo=timezone.utc),
            740.33,
            740.33,
            740.33,
            740.33,
            0,
            "5m",
            "ibkr:smart-rth",
        ),
        Bar(
            "SPY",
            datetime(2026, 6, 12, 16, 10, tzinfo=timezone.utc),
            740.4,
            741.2,
            740.1,
            741.0,
            1200,
            "5m",
            "ibkr",
        ),
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 12, 16, 20, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload("SPY"),
    )

    assert quality["status"] == "session_unknown"
    assert quality["signals_ok"] is False
    assert "gaps" not in quality
    assert "bad_bar_count" not in quality
    assert "bad_slots" not in quality
    assert "invalid_bars" not in quality
    assert "DATA BAD" not in quality["warning"]


def test_data_quality_accepts_flat_zero_volume_index_bar() -> None:
    bars = [
        Bar(
            "VIX",
            datetime(2026, 6, 12, 16, 0, tzinfo=timezone.utc),
            18.2,
            18.2,
            18.2,
            18.2,
            0,
            "5m",
            "ibkr",
        ),
        Bar(
            "VIX",
            datetime(2026, 6, 12, 16, 5, tzinfo=timezone.utc),
            18.2,
            18.2,
            18.2,
            18.2,
            0,
            "5m",
            "ibkr",
        ),
        Bar(
            "VIX",
            datetime(2026, 6, 12, 16, 10, tzinfo=timezone.utc),
            18.3,
            18.3,
            18.3,
            18.3,
            0,
            "5m",
            "ibkr",
        ),
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 12, 16, 20, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload(
            "VIX",
            con_id=13455763,
            asset_class="index",
            sec_type="IND",
        ),
    )

    assert "bad_bar_count" not in quality
    assert "DATA BAD" not in quality["warning"]


def test_data_quality_accepts_provider_confirmed_flat_zero_volume_etf_slots() -> None:
    bars = [
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 21, 50, tzinfo=timezone.utc),
            722.03,
            722.03,
            722.02,
            722.02,
            140,
            "5m",
            "ibkr",
        ),
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 21, 55, tzinfo=timezone.utc),
            722.02,
            722.02,
            722.02,
            722.02,
            0,
            "5m",
            "ibkr",
        ),
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 22, 0, tzinfo=timezone.utc),
            722.21,
            722.21,
            722.16,
            722.16,
            608,
            "5m",
            "ibkr",
        ),
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 15, 19, 40, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload("QQQ"),
    )

    assert "bad_bar_count" not in quality
    assert "gaps" not in quality
    assert "DATA BAD" not in quality["warning"]


def test_data_quality_does_not_infer_market_closed_without_provider_session_store(
    monkeypatch,
) -> None:
    bars = [
        Bar(
            "SPY",
            datetime(2026, 6, 16, 20, 0, tzinfo=timezone.utc),
            600.0,
            601.0,
            599.5,
            600.5,
            1000,
            "5m",
            "ibkr",
        ),
    ]
    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.provider_schedule_open_state",
        lambda *_args, **_kwargs: False,
    )

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 17, 1, 0, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload("SPY"),
    )

    assert quality["status"] == "stale+session_unknown"
    assert quality["market_closed"] is False
    assert "gaps" not in quality


def test_expected_market_closed_notice_blocks_unknown_provider_session(monkeypatch) -> None:
    from aef_terminal.data.provider_sessions import expected_market_closed_notice

    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.provider_schedule_open_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.next_provider_schedule_open",
        lambda *_args, **_kwargs: None,
    )

    notice = expected_market_closed_notice(
        datetime(2026, 7, 3, 20, 55, tzinfo=timezone.utc),
        datetime(2026, 7, 3, 21, 30, tzinfo=timezone.utc),
        "5m",
        store=object(),
        instrument=ibkr_future_payload("GC", exchange="COMEX"),
    )

    assert notice is not None
    assert notice["session"] == "session_unknown"
    assert "GC has no provider trading-session metadata" in notice["warning"]
    assert "latest 5m bar started at 2026-07-03T20:55:00+00:00" in notice["warning"]
    assert "bar ended at" not in notice["warning"]
    assert notice["reopen_ts"] is None


def test_data_quality_blocks_unknown_session_without_market_closed_fallback(monkeypatch) -> None:
    bars = [
        Bar(
            "GC",
            datetime(2026, 7, 3, 20, 55, tzinfo=timezone.utc),
            2400.0,
            2401.0,
            2399.0,
            2400.5,
            100,
            "5m",
            "ibkr",
        ),
    ]
    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.provider_schedule_open_state",
        lambda *_args, **_kwargs: None,
    )

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 7, 3, 21, 30, tzinfo=timezone.utc),
        store=object(),
        instrument=ibkr_future_payload("GC", exchange="COMEX"),
    )

    assert quality["status"] == "stale+session_unknown"
    assert quality["signals_ok"] is False
    assert quality["session_unknown"] is True
    assert quality["market_closed"] is False
    assert quality["session"] == "session_unknown"
    assert quality["reopen_ts"] is None


def test_data_quality_does_not_infer_bad_equity_bar_from_flat_zero_volume() -> None:
    bars = [
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 19, 25, tzinfo=timezone.utc),
            722.0,
            722.39,
            721.44,
            721.5,
            122412,
            "5m",
            "ibkr",
        ),
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 19, 30, tzinfo=timezone.utc),
            720.23,
            720.24,
            720.13,
            720.17,
            2866,
            "5m",
            "ibkr",
        ),
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 19, 35, tzinfo=timezone.utc),
            720.52,
            720.52,
            720.52,
            720.52,
            0,
            "5m",
            "ibkr",
        ),
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 12, 19, 40, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload("QQQ"),
    )

    assert quality["status"] == "session_unknown"
    assert quality["signals_ok"] is False
    assert "bad_bar_count" not in quality
    assert "invalid_bars" not in quality
    assert "DATA BAD" not in quality["warning"]


def test_data_quality_does_not_infer_after_hours_market_closed_without_provider_session_store(
    monkeypatch,
) -> None:
    bars = [
        Bar(
            "QQQ",
            datetime(2026, 6, 12, 23, 55, tzinfo=timezone.utc),
            722.0,
            722.3,
            721.8,
            722.1,
            2000,
            "5m",
            "ibkr:db-cache",
            closed=True,
        )
    ]
    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.provider_schedule_open_state",
        lambda *_args, **_kwargs: False,
    )

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 13, 0, 40, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload("QQQ"),
    )

    assert quality["status"] == "session_unknown"
    assert "gaps" not in quality


def test_async_market_snapshot_reads_db_without_scheduling_gap_repair(monkeypatch) -> None:
    start = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(
        days=1, hours=1
    )
    bars = [
        make_session_bar(start + timedelta(minutes=5 * index), index, 100.0) for index in range(40)
    ]

    async def fake_load(*_args, **_kwargs):
        return bars, ""

    async def fake_analyze(*_args, **_kwargs):
        return {
            "meta": {
                "data_quality": {
                    "status": "gap",
                    "gaps": [
                        {
                            "from": bars[-3].ts.isoformat(),
                            "to": bars[-1].ts.isoformat(),
                            "missing_bars": 2,
                        }
                    ],
                }
            }
        }

    def fail_schedule(*_args, **_kwargs):
        raise AssertionError("DB-only market snapshot must not schedule gap repair")

    monkeypatch.setattr(get_provider("ibkr"), "async_load_bars", fake_load)
    monkeypatch.setattr(
        snapshot_builder_module, "async_analyze_market_bars_for_snapshot", fake_analyze
    )
    monkeypatch.setattr(
        "aef_terminal.ui.services.chart_stream_gap_repair.schedule_provider_history_repair",
        fail_schedule,
    )

    snapshot = asyncio.run(
        analysis_module.async_build_market_snapshot(
            interval="5m",
            range_="1d",
            signal_range_="1d",
            instrument=ibkr_future_payload("ES"),
        )
    )

    assert "gap_repair" not in snapshot["meta"]


def test_coinbase_provider_is_authoritative_for_btc() -> None:
    base = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=60)
    base = base - timedelta(minutes=base.minute % 5)
    coinbase_bars = [
        Bar(
            "BTC-USD",
            base + timedelta(minutes=5 * index),
            100 + index,
            101 + index,
            99 + index,
            100.5 + index,
            1.0,
            "5m",
            "coinbase:BTC-USD",
            True,
        )
        for index in range(12)
    ]

    class CoinbaseHistoryStore:
        def read_bars_multi(self, *, timeframes, providers, start, end, instrument):
            assert timeframes == ["5m"]
            assert providers == ["coinbase"]
            assert start is not None
            assert end is not None
            assert instrument == coinbase_btc_payload()
            return {("coinbase", "5m"): coinbase_bars}

    bars, warning = get_provider("coinbase").load_bars(
        coinbase_btc_payload(),
        "5m",
        "1d",
        1.0,
        live_refresh=True,
        store=CoinbaseHistoryStore(),
    )
    quality = data_quality_report(
        bars,
        "5m",
        now_utc=base + timedelta(minutes=60),
        instrument=coinbase_btc_payload(),
    )
    snapshot = _analyze_bars(
        bars,
        display_symbol="BTC",
        analysis_as_of_utc=base + timedelta(minutes=60),
        indicator_params={"martin_carlo": {"enabled": False}},
        instrument=coinbase_btc_payload(),
    )
    stored_bars, stored_warning = get_provider("coinbase").load_bars(
        coinbase_btc_payload(),
        "5m",
        "1d",
        1.0,
        live_refresh=False,
        store=CoinbaseHistoryStore(),
    )

    assert warning == ""
    assert stored_warning == ""
    assert "gaps" not in quality
    assert len(bars) == 12
    assert {bar.symbol for bar in bars} == {"BTC-USD"}
    assert {bar.symbol for bar in stored_bars} == {"BTC-USD"}
    assert snapshot["meta"]["symbol"] == "BTC"
    assert snapshot["meta"]["provider_symbol"] == "BTC-USD"
    assert "data_symbol" not in snapshot["meta"]
    assert not hasattr(get_provider("coinbase"), "create_bar_feed")
    assert {bar.source for bar in bars} == {"coinbase:BTC-USD"}


def test_data_quality_does_not_infer_memorial_day_halt_without_provider_session_store() -> None:
    start = datetime(2026, 5, 25, 15, 30, tzinfo=timezone.utc)
    bars = [
        make_session_bar(start + timedelta(minutes=5 * index), index, 5000 + index * 0.3)
        for index in range(18)
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 5, 25, 17, 10, tzinfo=timezone.utc),
        instrument=ibkr_future_payload("ES"),
    )

    assert quality["status"] == "session_unknown"
    assert quality["signals_ok"] is False
    assert quality["market_closed"] is False
    assert quality["session"] == "session_unknown"
    assert "MARKET CLOSED" not in quality["warning"]
    assert quality["reopen_ts"] is None


def test_data_quality_uses_broker_hours_for_independence_day_halt() -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    sessions_module._TRADING_HOURS_CACHE.clear()
    with pytest.raises(RuntimeError, match="trading-hours store"):
        sessions_module._cached_provider_trading_hours(
            store=None, instrument=ibkr_future_payload("ES")
        )

    class Store:
        def initialize(self):
            return None

        def read_trading_hours(self, *, instrument: dict):
            assert instrument["provider"] == "ibkr"
            return {
                "timezone": "America/New_York",
                "trading_hours": "20260702:1800-20260703:1300;20260703:CLOSED;20260705:1800-20260706:1700",
                "liquid_hours": "20260702:1800-20260703:1300;20260703:CLOSED;20260705:1800-20260706:1700",
                "payload": {
                    "provider_contract_id": get_provider("ibkr").session_contract_id(instrument)
                },
            }

        def read_trading_session_open_state(
            self,
            ts: datetime,
            session_type: str,
            *,
            instrument: dict,
        ):
            assert instrument["provider"] == "ibkr"
            assert session_type in {"trading", "liquid"}
            if (
                datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc)
                <= ts
                < datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc)
            ):
                return True
            if (
                datetime(2026, 7, 3, 16, 0, tzinfo=timezone.utc)
                <= ts
                < datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc)
            ):
                return False
            return None

        def read_next_trading_session_open(
            self,
            ts: datetime,
            session_type: str,
            *,
            instrument: dict,
        ):
            assert instrument["provider"] == "ibkr"
            assert session_type in {"trading", "liquid"}
            if ts < datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc):
                return datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc)
            return None

    bars = [
        Bar(
            "ES",
            datetime(2026, 7, 3, 16, 55, tzinfo=timezone.utc),
            7557.0,
            7558.5,
            7556.0,
            7557.0,
            764,
            "5m",
            "ibkr:db-cache",
            closed=True,
        )
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 7, 3, 17, 20, tzinfo=timezone.utc),
        store=Store(),
        instrument=ibkr_future_payload("ES"),
    )

    assert quality["status"] == "market_closed"
    assert quality["market_closed"] is True
    assert "gaps" not in quality
    assert "DATA GAP" not in quality["warning"]
    assert "LIVE STALE" not in quality["warning"]
    assert quality["reopen_ts"] == datetime(
        2026,
        7,
        5,
        22,
        0,
        tzinfo=timezone.utc,
    )
    sessions_module._TRADING_HOURS_CACHE.clear()


def test_data_quality_does_not_infer_weekend_closed_without_provider_session_store() -> None:
    bars = [
        Bar(
            symbol="SPY",
            ts=datetime(2026, 5, 29, 23, 55, tzinfo=timezone.utc),
            open=755.0,
            high=756.0,
            low=754.0,
            close=755.5,
            volume=1000,
            timeframe="5m",
            source="test",
            closed=True,
        )
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        instrument=ibkr_stock_payload("SPY"),
    )

    assert quality["status"] == "stale+session_unknown"
    assert quality["market_closed"] is False
    assert "LIVE STALE" in quality["warning"]
    assert quality["reopen_ts"] is None


def test_ibkr_schedule_open_state_uses_normalized_intervals() -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    sessions_module._TRADING_HOURS_CACHE.clear()
    raw_reads = []

    class Store:
        def initialize(self):
            return None

        def read_trading_hours(self, *, instrument: dict):
            raw_reads.append(route_fingerprint(instrument))
            return {
                "payload": {
                    "provider_contract_id": get_provider("ibkr").session_contract_id(instrument)
                }
            }

        def read_trading_session_open_state(
            self,
            ts: datetime,
            session_type: str,
            *,
            instrument: dict,
        ):
            assert instrument["provider"] == "ibkr"
            if session_type == "trading" and ts == datetime(
                2026, 6, 1, 23, 59, tzinfo=timezone.utc
            ):
                return True
            if session_type == "trading" and ts == datetime(2026, 6, 2, 0, 1, tzinfo=timezone.utc):
                return False
            if session_type == "liquid" and ts == datetime(2026, 6, 1, 20, 1, tzinfo=timezone.utc):
                return False
            return None

    store = Store()

    with pytest.raises(RuntimeError, match="trading-hours store"):
        provider_schedule_open_state(
            datetime(2026, 6, 1, 23, 59, tzinfo=timezone.utc),
            False,
            instrument=ibkr_stock_payload("SPY", con_id=756733),
        )
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    assert (
        provider_schedule_open_state(
            datetime(2026, 6, 1, 23, 59, tzinfo=timezone.utc),
            False,
            store=store,
            instrument=instrument,
        )
        is True
    )
    assert (
        provider_schedule_open_state(
            datetime(2026, 6, 2, 0, 1, tzinfo=timezone.utc),
            False,
            store=store,
            instrument=instrument,
        )
        is False
    )
    assert (
        provider_schedule_open_state(
            datetime(2026, 6, 1, 20, 1, tzinfo=timezone.utc),
            True,
            store=store,
            instrument=instrument,
        )
        is False
    )
    assert raw_reads == [route_fingerprint(instrument)]
    sessions_module._TRADING_HOURS_CACHE.clear()


def test_analyze_bars_does_not_synthesize_bars_without_provider_session_slots() -> None:
    bars = [
        make_session_bar(datetime(2026, 5, 28, 19, 40, tzinfo=timezone.utc), 0, 5900.0),
        make_session_bar(datetime(2026, 5, 28, 22, 0, tzinfo=timezone.utc), 1, 5901.0),
    ]

    snapshot = analyze_bars(bars, instrument=ibkr_future_payload("ES"))
    missing = [bar for bar in snapshot["bars"] if bar.get("missing")]

    assert len(missing) == 0


def test_data_quality_accepts_contiguous_quote_live_bar() -> None:
    bars = [
        Bar(
            "QQQ",
            datetime(2026, 6, 1, 16, 20, tzinfo=timezone.utc),
            740.0,
            740.8,
            739.8,
            740.63,
            1000,
            "5m",
            "ibkr",
        ),
        Bar(
            "QQQ",
            datetime(2026, 6, 1, 16, 25, tzinfo=timezone.utc),
            740.63,
            741.04,
            740.63,
            741.04,
            0,
            "5m",
            "ibkr:db-cache+quote-live",
            closed=False,
        ),
    ]

    quality = data_quality_report(
        bars,
        "5m",
        now_utc=datetime(2026, 6, 1, 16, 29, tzinfo=timezone.utc),
        store=OpenIbkrSessionStore(),
        instrument=ibkr_stock_payload("QQQ"),
    )

    assert quality["status"] == "ok"
    assert quality["signals_ok"] is True
    assert "gaps" not in quality
