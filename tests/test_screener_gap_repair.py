from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data.provider_contract import (
    HistoryRepairAdmission,
    HistoryRepairAdmissionStatus,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairOutcomeStatus,
    HistoryRepairPriority,
    HistoryRequestAdmissionIdentity,
)
from aef_terminal.domain import BarProviderRequest
from aef_terminal.runtime.timeframes import HistoryRangeWindow
from aef_terminal.data.history_coverage import (
    HistoryCoverageReceipt,
    current_history_coverage_contract,
)
from aef_terminal.ui import screener_gap_repair


START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW = HistoryRangeWindow("31d", START, END)


def _identity() -> HistoryRequestAdmissionIdentity:
    return HistoryRequestAdmissionIdentity(
        request_contract_version=1,
        admission_contract_version=1,
        request_mode="get_candles_exchange",
        request_type=BarProviderRequest.HISTORICAL,
        provider_source="EXCHANGE",
        provider_contract_id="uid-1",
        provider_contract_type="INSTRUMENT_UID",
        data_type="TRADES",
    )


def _contract():
    identity = _identity()
    return current_history_coverage_contract(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        route_fingerprint="route-1",
        timeframe="5m",
        request_mode=identity.request_mode,
        request_type=identity.request_type.value,
        provider_source=identity.provider_source,
        provider_contract_id=identity.provider_contract_id,
        provider_contract_type=identity.provider_contract_type,
        data_type=identity.data_type,
    )


def _receipt(starts_at: datetime, ends_at: datetime) -> HistoryCoverageReceipt:
    return HistoryCoverageReceipt(
        contract=_contract(),
        covered_from=starts_at,
        covered_to=ends_at,
        response_count=0,
        completed_at=END + timedelta(days=1),
    )


class _Store:
    def __init__(self, receipts: tuple[HistoryCoverageReceipt, ...] = ()) -> None:
        self.receipts = receipts
        self.reads: list[tuple[Any, datetime, datetime]] = []

    def read_history_coverage(
        self,
        contract: Any,
        *,
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[HistoryCoverageReceipt, ...]:
        self.reads.append((contract, starts_at, ends_at))
        return self.receipts


def _supported_route() -> SimpleNamespace:
    instrument = {"provider": "tinvest", "instrument_id": "tinvest:uid-1"}
    return SimpleNamespace(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        fingerprint="route-1",
        instrument=instrument,
        adapter=SimpleNamespace(
            capabilities=SimpleNamespace(
                gap_repair=True,
                exact_history_snapshot_authority=True,
            ),
            history_request_identity=lambda _instrument: _identity(),
            history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
        ),
    )


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        screener_gap_repair,
        "history_lookback_window",
        lambda days, interval, **_kwargs: (
            WINDOW if (days, interval) == (31, "5m") else pytest.fail("unexpected watchlist window")
        ),
    )
    tail = SimpleNamespace(ts=END - timedelta(minutes=5), closed=True)
    monkeypatch.setattr(
        screener_gap_repair,
        "read_recent_provider_bars",
        lambda *_args, **_kwargs: [tail],
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda bars, interval, **_kwargs: {
            "status": "ok",
            "signals_ok": True,
            "latest_ts": bars[-1].ts,
            "interval": interval,
        },
    )


def test_unsupported_provider_keeps_session_quality_without_history_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = {"provider": "ibkr", "instrument_id": "ibkr:1"}
    route = SimpleNamespace(
        provider="ibkr",
        instrument_id="ibkr:1",
        fingerprint="ibkr-route",
        instrument=instrument,
        adapter=SimpleNamespace(capabilities=SimpleNamespace(gap_repair=False)),
    )
    store = object()
    bar = SimpleNamespace(ts=END - timedelta(minutes=5), closed=True)
    _freeze(monkeypatch)
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(
        screener_gap_repair,
        "read_recent_provider_bars",
        lambda actual_route, interval, *, store, limit: (
            [bar]
            if (actual_route, interval, store, limit) == (route, "5m", store, 1)
            else pytest.fail("unexpected latest-bar read")
        ),
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda bars, interval, **_kwargs: {
            "status": "ok",
            "signals_ok": True,
            "latest_ts": bars[-1].ts,
            "interval": interval,
        },
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "select_chart_history_repair",
        lambda **_kwargs: pytest.fail("unsupported provider must not plan history"),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        instrument,
        "5m",
    )

    assert result["status"] == "ok"
    assert result["latest_ts"] == bar.ts
    assert result["history_coverage"]["state"] == "unsupported"
    assert result["history_coverage"]["repair"]["status"] == "not_supported"


def test_fresh_watchlist_tail_does_not_read_receipts_or_schedule_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    store = _Store((_receipt(START, END),))
    _freeze(monkeypatch)
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(screener_gap_repair, "chart_bars_updated_generation", lambda *_a, **_k: 2)
    monkeypatch.setattr(
        screener_gap_repair,
        "schedule_chart_cached_gap_repair",
        lambda *_a, **_k: pytest.fail("covered range must not schedule"),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
    )

    assert result["signals_ok"] is True
    assert result["history_coverage"]["state"] == "not_checked"
    assert result["history_coverage"]["provider_range_complete"] is False
    assert result["history_coverage"]["bar_repair"]["status"] == "not_needed"
    assert store.reads == []


def test_partial_watchlist_coverage_schedules_background_exact_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    covered_to = START + timedelta(days=10)
    store = _Store((_receipt(START, covered_to),))
    intent = HistoryRepairIntent(
        provider="tinvest",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        timeframe="5m",
        starts_at=END - timedelta(days=1),
        ends_at=END,
        request_identity=_identity(),
        canonical_generation=5,
        priority=HistoryRepairPriority.BACKGROUND,
        cooldown_seconds=0.0,
    )
    selected: dict[str, Any] = {}
    _freeze(monkeypatch)
    stale_tail = SimpleNamespace(ts=END - timedelta(minutes=30), closed=True)
    monkeypatch.setattr(
        screener_gap_repair,
        "read_recent_provider_bars",
        lambda *_args, **_kwargs: [stale_tail],
    )
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(screener_gap_repair, "chart_bars_updated_generation", lambda *_a, **_k: 5)
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda *_args, **_kwargs: {
            "status": "stale",
            "signals_ok": False,
            "market_closed": False,
            "session_unknown": False,
            "session_warmup": False,
        },
    )

    def select(**kwargs):
        selected.update(kwargs)
        return intent

    monkeypatch.setattr(screener_gap_repair, "select_chart_history_repair", select)
    monkeypatch.setattr(
        screener_gap_repair,
        "schedule_chart_cached_gap_repair",
        lambda candidate, **_kwargs: HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=candidate,
            newly_admitted=True,
        ),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
    )

    assert result["signals_ok"] is False
    assert result["status"] == "stale"
    assert result["history_coverage"]["state"] == "not_checked"
    assert result["history_coverage"]["pending_from"] is None
    assert result["history_coverage"]["pending_to"] is None
    assert result["history_coverage"]["repair"]["status"] == "scheduled"
    assert selected["priority"] is HistoryRepairPriority.BACKGROUND
    assert selected["requested_from"] == stale_tail.ts + timedelta(minutes=5)
    assert selected["requested_to"] == END
    assert selected["classify_small_recent_gap"] is True
    assert "eligible_receipts" not in selected
    assert store.reads == []


def test_provider_reconnect_requests_only_verified_missing_open_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    store = _Store()
    latest = SimpleNamespace(ts=END - timedelta(hours=8), closed=True)
    expected_slots = tuple(latest.ts + timedelta(minutes=offset) for offset in (5, 10, 15))
    intent = HistoryRepairIntent(
        provider=route.provider,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        timeframe="5m",
        starts_at=expected_slots[0],
        ends_at=expected_slots[-1] + timedelta(minutes=5),
        request_identity=_identity(),
        canonical_generation=5,
        priority=HistoryRepairPriority.BACKGROUND,
        cooldown_seconds=0.0,
    )
    selected: dict[str, Any] = {}
    _freeze(monkeypatch)
    monkeypatch.setattr(
        screener_gap_repair,
        "read_recent_provider_bars",
        lambda *_args, **_kwargs: [latest],
    )
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(screener_gap_repair, "chart_bars_updated_generation", lambda *_a, **_k: 5)
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda *_args, **_kwargs: {
            "status": "market_closed",
            "signals_ok": False,
            "market_closed": True,
            "session_warmup": False,
        },
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: SimpleNamespace(
            schedule_state="verified",
            truncated=False,
            expected_slots=expected_slots,
        ),
    )

    def select(**kwargs):
        selected.update(kwargs)
        return intent

    monkeypatch.setattr(screener_gap_repair, "select_chart_history_repair", select)
    monkeypatch.setattr(
        screener_gap_repair,
        "schedule_chart_cached_gap_repair",
        lambda candidate, **_kwargs: HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=candidate,
            newly_admitted=True,
        ),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
        explicit_recovery_tail=True,
    )

    assert result["status"] == "market_closed"
    assert result["history_coverage"]["bar_repair"]["status"] == "scheduled"
    assert selected["requested_from"] == expected_slots[0]
    assert selected["requested_to"] == expected_slots[-1] + timedelta(minutes=5)
    assert selected["classify_small_recent_gap"] is False


def test_provider_reconnect_skips_verified_closed_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    store = _Store()
    latest = SimpleNamespace(ts=END - timedelta(hours=8), closed=True)
    _freeze(monkeypatch)
    monkeypatch.setattr(
        screener_gap_repair,
        "read_recent_provider_bars",
        lambda *_args, **_kwargs: [latest],
    )
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(screener_gap_repair, "chart_bars_updated_generation", lambda *_a, **_k: 5)
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda *_args, **_kwargs: {
            "status": "market_closed",
            "signals_ok": False,
            "market_closed": True,
            "session_warmup": False,
        },
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: SimpleNamespace(
            schedule_state="verified",
            truncated=False,
            expected_slots=(),
        ),
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "select_chart_history_repair",
        lambda **_kwargs: pytest.fail("verified closed slots must not request provider history"),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
        explicit_recovery_tail=True,
    )

    assert result["history_coverage"]["bar_repair"]["status"] == "not_needed"


def test_partial_coverage_alone_does_not_trigger_bar_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    store = _Store((_receipt(START, START + timedelta(days=10)),))
    _freeze(monkeypatch)
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(
        screener_gap_repair,
        "select_chart_history_repair",
        lambda **_kwargs: pytest.fail("receipt absence alone must not plan returned-bar repair"),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
    )

    assert result["signals_ok"] is True
    assert result["history_coverage"]["state"] == "not_checked"
    assert result["history_coverage"]["bar_repair"]["status"] == "not_needed"
    assert store.reads == []


def test_next_screener_payload_exposes_terminal_repair_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    store = _Store()
    intent = HistoryRepairIntent(
        provider=route.provider,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        timeframe="5m",
        starts_at=END - timedelta(days=1),
        ends_at=END,
        request_identity=_identity(),
        canonical_generation=5,
        priority=HistoryRepairPriority.BACKGROUND,
        cooldown_seconds=0.0,
    )
    _freeze(monkeypatch)
    monkeypatch.setattr(
        screener_gap_repair,
        "read_recent_provider_bars",
        lambda *_args, **_kwargs: [SimpleNamespace(ts=END - timedelta(minutes=30), closed=True)],
    )
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(screener_gap_repair, "chart_bars_updated_generation", lambda *_a, **_k: 5)
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda *_args, **_kwargs: {
            "status": "stale",
            "signals_ok": False,
            "market_closed": False,
            "session_unknown": False,
            "session_warmup": False,
        },
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "select_chart_history_repair",
        lambda **_kwargs: intent,
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "schedule_chart_cached_gap_repair",
        lambda candidate, **_kwargs: HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.THROTTLED,
            intent=candidate,
            retry_after_seconds=30.0,
        ),
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "latest_chart_history_repair_outcome",
        lambda candidate: HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.FETCH_FAILED,
            intent=candidate,
            error_code="PROVIDER_TIMEOUT",
        ),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
    )

    repair = result["history_coverage"]["bar_repair"]
    assert repair["phase"] == "terminal"
    assert repair["status"] == "fetch_failed"
    assert repair["error_code"] == "PROVIDER_TIMEOUT"
    assert repair["requested_from"] == intent.starts_at.isoformat()
    assert repair["requested_to"] == intent.ends_at.isoformat()


def test_generation_race_does_not_enqueue_watchlist_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    store = _Store()
    generations = iter((8, 9))
    _freeze(monkeypatch)
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", lambda: store)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)
    monkeypatch.setattr(
        screener_gap_repair,
        "data_quality_report",
        lambda *_args, **_kwargs: {
            "status": "stale",
            "signals_ok": False,
            "market_closed": False,
            "session_unknown": False,
            "session_warmup": False,
        },
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "chart_bars_updated_generation",
        lambda *_a, **_k: next(generations),
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "select_chart_history_repair",
        lambda **_kwargs: HistoryRepairIntent(
            provider="tinvest",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe="5m",
            starts_at=START,
            ends_at=END,
            request_identity=_identity(),
            canonical_generation=8,
            priority=HistoryRepairPriority.BACKGROUND,
            cooldown_seconds=0.0,
        ),
    )
    monkeypatch.setattr(
        screener_gap_repair,
        "schedule_chart_cached_gap_repair",
        lambda *_a, **_k: pytest.fail("stale generation must not schedule"),
    )

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
    )

    assert result["history_coverage"]["repair"]["status"] == "stale_snapshot"


def test_missing_store_returns_typed_unavailable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _supported_route()
    _freeze(monkeypatch)
    monkeypatch.setattr(screener_gap_repair, "_STORE_FACTORY", None)
    monkeypatch.setattr(screener_gap_repair, "route_instrument", lambda *_args: route)

    result = screener_gap_repair._watchlist_gap_quality_for_instrument(
        route.instrument,
        "5m",
    )

    assert result["status"] == "storage_unavailable"
    assert result["history_coverage"]["state"] == "unknown"
