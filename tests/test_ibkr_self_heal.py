from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.ui.services.ibkr_self_heal import (
    IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS,
    IBKR_SELF_HEAL_MAX_RECONNECTS,
    IbkrSelfHealState,
    IbkrSelfHealIssue,
    detect_ibkr_self_heal_issue,
    run_ibkr_self_heal_loop,
)
from tests.provider_payloads import coinbase_btc_payload, ibkr_stock_payload

SPY_INSTRUMENT_ID = "instrument:spy"
ES_INSTRUMENT_ID = "instrument:es"
GC_INSTRUMENT_ID = "instrument:gc"
SPY_ROUTE = "route:spy"
ES_ROUTE = "route:es"
GC_ROUTE = "route:gc"
QQQ_ROUTE = "route:qqq"


def test_ibkr_self_heal_rejects_non_ibkr_chart_and_quote_demand(monkeypatch) -> None:
    coinbase = coinbase_btc_payload()
    ibkr = ibkr_stock_payload("SPY", con_id=756733)
    reconnects: list[list[dict]] = []

    async def force_reconnect(*, quote_instruments, timeout):
        _ = timeout
        reconnects.append(list(quote_instruments))
        return {"ok": True}

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_first_iteration,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [(coinbase["instrument_id"], "5m", "3d", 1)],
                quote_instruments=lambda: [coinbase],
                instrument_lookup=lambda instrument_id: (
                    coinbase if instrument_id == coinbase["instrument_id"] else ibkr
                ),
                ibkr_status=lambda: {
                    "quote_connected": True,
                    "chart_connected": True,
                    "quote_values": 0,
                    "quote_subscriptions": 0,
                },
                watchlist_gap_quality=lambda *_args: {
                    "stale_minutes": 120.0,
                    "stale_threshold_minutes": 15.0,
                },
                force_reconnect=force_reconnect,
                state=IbkrSelfHealState(),
            )
        )

    assert reconnects == []


def test_ibkr_history_connection_generation_wakes_schedule_recovery(monkeypatch) -> None:
    generations = iter((7, 8))
    recoveries: list[bool] = []
    sleeps = 0

    async def force_reconnect(**_kwargs):
        raise AssertionError("healthy history recovery must not force another reconnect")

    async def stop_after_recovery(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_recovery,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [],
                quote_instruments=lambda: [],
                instrument_lookup=lambda _instrument_id: {},
                ibkr_status=lambda: {
                    "status": "history_only",
                    "ok": True,
                    "history_connected": True,
                    "history_connection_generation": next(generations),
                },
                watchlist_gap_quality=lambda *_args: {},
                force_reconnect=force_reconnect,
                history_session_recovered=lambda: recoveries.append(True),
                state=IbkrSelfHealState(),
            )
        )

    assert recoveries == [True]


def test_ibkr_self_heal_repairs_quarantine_without_active_market_demand() -> None:
    issue = detect_ibkr_self_heal_issue(
        {
            "status": "live",
            "ok": True,
            "quarantined_session_count": 1,
            "quarantined_session_scopes": {"quote_probe": 1},
            "quarantined_persistent_sessions": [],
        },
        [],
        {},
        [],
    )

    assert issue is not None
    assert issue.code == "session_quarantined"
    assert issue.details == {
        "quarantined_session_count": 1,
        "quarantined_session_scopes": {"quote_probe": 1},
        "quarantined_persistent_sessions": [],
    }


def test_ibkr_self_heal_cleans_transient_quarantine_without_global_reconnect(
    monkeypatch,
) -> None:
    cleanups: list[float] = []
    reconnects: list[bool] = []
    state = IbkrSelfHealState()

    async def cleanup_quarantine(*, timeout):
        cleanups.append(timeout)
        return {"ok": True, "quarantined_after": 0}

    async def force_reconnect(**_kwargs):
        reconnects.append(True)
        return {"ok": True}

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_first_iteration,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [],
                quote_instruments=lambda: [],
                instrument_lookup=lambda _instrument_id: {},
                ibkr_status=lambda: {
                    "status": "live",
                    "ok": True,
                    "quarantined_session_count": 1,
                    "quarantined_session_scopes": {"quote_probe": 1},
                    "quarantined_persistent_sessions": [],
                },
                watchlist_gap_quality=lambda *_args: {},
                force_reconnect=force_reconnect,
                cleanup_quarantine=cleanup_quarantine,
                state=state,
            )
        )

    assert cleanups == [2.5]
    assert reconnects == []
    assert state.attempt_times == []


def test_ibkr_self_heal_detects_stale_active_chart_while_connected() -> None:
    issue = detect_ibkr_self_heal_issue(
        {"quote_connected": True, "quote_values": 2},
        [(SPY_INSTRUMENT_ID, "5m", "3d", 1)],
        {
            (SPY_INSTRUMENT_ID, "5m"): {
                "stale_minutes": 120.0,
                "stale_threshold_minutes": 15.0,
                "market_closed": False,
                "warning": "latest bar is behind",
            }
        },
        [SPY_ROUTE],
    )

    assert issue is not None
    assert issue.code == "active_chart_stale"
    assert issue.chart_instrument_id == SPY_INSTRUMENT_ID


def test_ibkr_self_heal_ignores_market_closed_stale_chart() -> None:
    issue = detect_ibkr_self_heal_issue(
        {"quote_connected": True, "quote_values": 2},
        [(SPY_INSTRUMENT_ID, "5m", "3d", 1)],
        {
            (SPY_INSTRUMENT_ID, "5m"): {
                "stale_minutes": 120.0,
                "stale_threshold_minutes": 15.0,
                "market_closed": True,
            }
        },
        [SPY_ROUTE],
    )

    assert issue is None


def test_ibkr_self_heal_ignores_first_confirmed_bar_session_warmup() -> None:
    issue = detect_ibkr_self_heal_issue(
        {"quote_connected": True, "quote_values": 2},
        [(SPY_INSTRUMENT_ID, "5m", "3d", 1)],
        {
            (SPY_INSTRUMENT_ID, "5m"): {
                "stale_minutes": 802.0,
                "stale_threshold_minutes": 90.0,
                "market_closed": False,
                "session_warmup": True,
            }
        },
        [SPY_ROUTE],
    )

    assert issue is None


def test_ibkr_self_heal_waits_for_active_history_repair_before_reconnecting_chart() -> None:
    issue = detect_ibkr_self_heal_issue(
        {
            "quote_connected": True,
            "quote_values": 2,
            "market_data_manager": {
                "lanes": {
                    "history": {
                        "running": True,
                        "running_seconds": 2.0,
                        "pending": 0,
                    }
                }
            },
        },
        [(SPY_INSTRUMENT_ID, "5m", "3d", 1)],
        {
            (SPY_INSTRUMENT_ID, "5m"): {
                "stale_minutes": 120.0,
                "stale_threshold_minutes": 90.0,
                "market_closed": False,
            }
        },
        [SPY_ROUTE],
    )

    assert issue is None


def test_ibkr_self_heal_detects_connected_quote_session_without_values() -> None:
    now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)
    issue = detect_ibkr_self_heal_issue(
        {
            "quote_connected": True,
            "quote_subscriptions": 4,
            "quote_values": 0,
            "last_quote_sync_at": (now - timedelta(seconds=90)).isoformat(),
        },
        [],
        {},
        [SPY_ROUTE, GC_ROUTE],
        now_wall=now,
    )

    assert issue is not None
    assert issue.code == "connected_no_quotes"


def test_ibkr_self_heal_detects_dead_quote_session_with_chart_connected() -> None:
    issue = detect_ibkr_self_heal_issue(
        {
            "chart_connected": True,
            "quote_connected": False,
            "quote_subscriptions": 0,
            "quote_values": 0,
            "market_data_manager": {
                "lanes": {
                    "quote": {
                        "last_error": "IBKR lane request timed out",
                        "error": {
                            "code": "IBKR_LANE_TIMEOUT",
                            "message": "IBKR lane request timed out",
                        },
                    }
                }
            },
        },
        [(ES_INSTRUMENT_ID, "5m", "1d", 1)],
        {},
        [ES_ROUTE, SPY_ROUTE, QQQ_ROUTE],
    )

    assert issue is not None
    assert issue.code == "quote_session_down"
    assert issue.details["quote_route_fingerprints"] == [ES_ROUTE, SPY_ROUTE, QQQ_ROUTE]
    assert issue.details["quote_lane_error"] == "IBKR lane request timed out"


def test_ibkr_self_heal_waits_for_nonstalled_connection_bootstrap() -> None:
    status = {
        "quote_connected": False,
        "chart_connected": False,
        "history_connected": False,
        "connection_in_progress": {"history": True},
        "market_data_manager": {
            "lanes": {
                "history": {
                    "running": True,
                    "running_seconds": 2.0,
                    "running_timeout_seconds": 5.0,
                    "pending": 0,
                }
            }
        },
    }

    streams = [(SPY_INSTRUMENT_ID, "5m", "3d", 1)]
    assert detect_ibkr_self_heal_issue(status, streams, {}, []) is None

    status["market_data_manager"]["lanes"]["history"].update(
        {
            "running": False,
            "running_seconds": 0.0,
            "last_error": "IBKR bootstrap failed",
        }
    )
    status["connection_in_progress"]["history"] = False
    issue = detect_ibkr_self_heal_issue(status, streams, {}, [])

    assert issue is not None
    assert issue.code == "connection_down"

    status["market_data_manager"]["lanes"]["history"]["running"] = True
    status["market_data_manager"]["lanes"]["history"]["running_seconds"] = 46.0
    issue = detect_ibkr_self_heal_issue(status, streams, {}, [])

    assert issue is not None
    assert issue.code == "lane_running_stalled"


def test_ibkr_self_heal_waits_for_nonstalled_quote_bootstrap() -> None:
    status = {
        "chart_connected": True,
        "quote_connected": False,
        "quote_subscriptions": 0,
        "quote_values": 0,
        "connection_in_progress": {"quote": True},
        "market_data_manager": {
            "lanes": {
                "quote": {
                    "running": False,
                    "running_seconds": 0.0,
                    "pending": 1,
                    "oldest_pending_seconds": 2.0,
                }
            }
        },
    }

    assert detect_ibkr_self_heal_issue(status, [], {}, [SPY_ROUTE]) is None

    status["market_data_manager"]["lanes"]["quote"]["oldest_pending_seconds"] = 61.0
    issue = detect_ibkr_self_heal_issue(status, [], {}, [SPY_ROUTE])

    assert issue is not None
    assert issue.code == "lane_queue_stalled"


def test_ibkr_self_heal_unrelated_manager_work_does_not_hide_dead_quote_session() -> None:
    issue = detect_ibkr_self_heal_issue(
        {
            "chart_connected": True,
            "quote_connected": False,
            "connection_in_progress": {"history": True, "quote": False},
            "market_data_manager": {
                "lanes": {
                    "history": {"running": True, "running_seconds": 2.0},
                    "gex": {"running": True, "running_seconds": 2.0},
                }
            },
        },
        [],
        {},
        [SPY_ROUTE],
    )

    assert issue is not None
    assert issue.code == "quote_session_down"


def test_ibkr_self_heal_loop_does_not_compete_with_quote_bootstrap(monkeypatch) -> None:
    ibkr = ibkr_stock_payload("SPY", con_id=756733)
    reconnects: list[list[dict]] = []

    async def force_reconnect(*, quote_instruments, timeout):
        _ = timeout
        reconnects.append(list(quote_instruments))
        return {"ok": True}

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_first_iteration,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [],
                quote_instruments=lambda: [ibkr],
                instrument_lookup=lambda _instrument_id: ibkr,
                ibkr_status=lambda: {
                    "quote_connected": False,
                    "chart_connected": False,
                    "history_connected": False,
                    "connection_in_progress": {"quote": True},
                    "market_data_manager": {
                        "lanes": {
                            "quote": {
                                "running": True,
                                "running_seconds": 2.0,
                                "running_timeout_seconds": 5.0,
                                "pending": 0,
                            }
                        }
                    },
                },
                watchlist_gap_quality=lambda *_args: {},
                force_reconnect=force_reconnect,
                state=IbkrSelfHealState(),
            )
        )

    assert reconnects == []


def test_successful_self_heal_requests_bounded_watchlist_recovery_tails(monkeypatch) -> None:
    spy = ibkr_stock_payload("SPY", con_id=756733)
    qqq = ibkr_stock_payload("QQQ", con_id=320227571)
    repairs: list[tuple[str, str]] = []
    lifecycle: list[str] = []
    state = IbkrSelfHealState()

    async def force_reconnect(*, quote_instruments, timeout):
        assert quote_instruments == [spy, qqq]
        assert timeout == 2.5
        lifecycle.append("reconnect")
        return {"ok": True, "status": {"history_connection_generation": 2}}

    def request_recovery_tail(instrument_id: str, interval: str) -> dict[str, object]:
        repairs.append((instrument_id, interval))
        lifecycle.append(f"tail:{instrument_id}")
        return {
            "history_coverage": {
                "repair": {
                    "phase": "admission",
                    "status": "scheduled",
                }
            }
        }

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_first_iteration,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [
                    (spy["instrument_id"], "5m", "5d", 1),
                ],
                quote_instruments=lambda: [spy, qqq],
                instrument_lookup=lambda instrument_id: (
                    spy if instrument_id == spy["instrument_id"] else qqq
                ),
                ibkr_status=lambda: {
                    "quote_connected": True,
                    "quote_values": 1,
                    "history_connection_generation": 1,
                },
                watchlist_gap_quality=lambda *_args: {
                    "status": "stale",
                    "stale_minutes": 120.0,
                    "stale_threshold_minutes": 20.0,
                    "market_closed": False,
                },
                request_recovery_tail=request_recovery_tail,
                force_reconnect=force_reconnect,
                history_session_recovered=lambda: lifecycle.append("recovered"),
                state=state,
            )
        )

    assert repairs == [
        (spy["instrument_id"], "5m"),
        (qqq["instrument_id"], "5m"),
    ]
    assert lifecycle == [
        "reconnect",
        f"tail:{spy['instrument_id']}",
        f"tail:{qqq['instrument_id']}",
        "recovered",
    ]
    assert state.last_result_ok is True
    assert state.last_error == ""


def test_quote_only_self_heal_does_not_invent_chart_recovery_interval(monkeypatch) -> None:
    spy = ibkr_stock_payload("SPY", con_id=756733)
    repairs: list[tuple[str, str]] = []
    state = IbkrSelfHealState()

    async def force_reconnect(*, quote_instruments, timeout):
        assert quote_instruments == [spy]
        assert timeout == 2.5
        return {"ok": True}

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_first_iteration,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [],
                quote_instruments=lambda: [spy],
                instrument_lookup=lambda _instrument_id: spy,
                ibkr_status=lambda: {
                    "chart_connected": True,
                    "quote_connected": False,
                    "quote_subscriptions": 0,
                    "quote_values": 0,
                },
                watchlist_gap_quality=lambda *_args: {},
                request_recovery_tail=lambda instrument_id, interval: repairs.append(
                    (instrument_id, interval)
                ),
                force_reconnect=force_reconnect,
                state=state,
            )
        )

    assert repairs == []
    assert state.last_result_ok is True
    assert state.last_error == ""


@pytest.mark.parametrize(
    ("failure_mode", "expected_failure"),
    (("typed", "storage_unavailable"), ("exception", "tail request failed")),
)
def test_self_heal_reports_recovery_tail_admission_failure(
    monkeypatch,
    failure_mode: str,
    expected_failure: str,
) -> None:
    spy = ibkr_stock_payload("SPY", con_id=756733)
    state = IbkrSelfHealState()

    async def force_reconnect(*, quote_instruments, timeout):
        assert quote_instruments == [spy]
        assert timeout == 2.5
        return {"ok": True}

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    def request_recovery_tail(*_args):
        if failure_mode == "exception":
            raise RuntimeError("tail request failed")
        return {
            "status": "storage_unavailable",
            "history_coverage": {
                "repair": {
                    "phase": "idle",
                    "status": "storage_unavailable",
                }
            },
        }

    monkeypatch.setattr(
        "aef_terminal.ui.services.ibkr_self_heal.asyncio.sleep",
        stop_after_first_iteration,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_ibkr_self_heal_loop(
                poll_seconds=1.0,
                server_sleeping=lambda: False,
                active_chart_streams=lambda: [(spy["instrument_id"], "5m", "5d", 1)],
                quote_instruments=lambda: [spy],
                instrument_lookup=lambda _instrument_id: spy,
                ibkr_status=lambda: {
                    "quote_connected": True,
                    "quote_values": 1,
                },
                watchlist_gap_quality=lambda *_args: {
                    "status": "stale",
                    "stale_minutes": 120.0,
                    "stale_threshold_minutes": 20.0,
                    "market_closed": False,
                },
                request_recovery_tail=request_recovery_tail,
                force_reconnect=force_reconnect,
                state=state,
            )
        )

    assert state.last_result_ok is False
    assert state.last_error == (
        f"IBKR recovery tail failed: {spy['instrument_id']} 5m: {expected_failure}"
    )


def test_ibkr_self_heal_detects_stalled_manager_lane() -> None:
    issue = detect_ibkr_self_heal_issue(
        {
            "history_connected": True,
            "market_data_manager": {
                "lanes": {
                    "history": {
                        "running": True,
                        "running_seconds": 75.0,
                        "pending": 0,
                    }
                }
            },
        },
        [(GC_INSTRUMENT_ID, "5m", "3d", 1)],
        {},
        [],
    )

    assert issue is not None
    assert issue.code == "lane_running_stalled"
    assert issue.details["lane"] == "history"


def test_ibkr_self_heal_respects_active_lane_timeout_before_declaring_stall() -> None:
    status = {
        "history_connected": True,
        "market_data_manager": {
            "lanes": {
                "gex": {
                    "running": True,
                    "running_seconds": 60.0,
                    "running_timeout_seconds": 58.0,
                    "pending": 0,
                }
            }
        },
    }

    assert detect_ibkr_self_heal_issue(status, [(ES_INSTRUMENT_ID, "5m", "3d", 1)], {}, []) is None

    status["market_data_manager"]["lanes"]["gex"]["running_seconds"] = 70.0
    issue = detect_ibkr_self_heal_issue(status, [(ES_INSTRUMENT_ID, "5m", "3d", 1)], {}, [])

    assert issue is not None
    assert issue.code == "lane_running_stalled"
    assert issue.details["stuck_threshold_seconds"] == 68.0


def test_ibkr_self_heal_reconnect_budget_uses_short_cooldown_after_burst() -> None:
    state = IbkrSelfHealState()

    for now in (100.0, 150.0, 200.0):
        allowed, reason = state.reconnect_allowed(now)
        assert allowed is True
        assert reason == ""
        state.record_attempt(now)

    allowed, reason = state.reconnect_allowed(250.0)

    assert allowed is False
    assert reason == "reconnect_cooldown"
    assert state.blocked_since is None
    assert state.blocked_reason == ""

    allowed, reason = state.reconnect_allowed(200.0 + IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS)

    assert allowed is True
    assert reason == ""
    assert len(state.attempt_times) == IBKR_SELF_HEAL_MAX_RECONNECTS - 1


def test_ibkr_self_heal_healthy_check_clears_reconnect_budget() -> None:
    state = IbkrSelfHealState()
    issue = IbkrSelfHealIssue(code="connected_no_quotes", reason="stale quotes")
    state.record_check(issue)
    state.record_attempt(100.0)
    state.record_attempt(150.0)

    state.record_check(None)

    assert state.last_issue is None
    assert state.attempt_times == []
    assert state.blocked_since is None
    assert state.blocked_reason == ""
