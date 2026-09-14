from __future__ import annotations

from aef_terminal.ui.routers.error_payloads import (
    build_error_payload,
    build_stream_status_payload,
    build_action_error_response,
)


def test_build_error_payload_default_shape() -> None:
    payload = build_error_payload(
        code="TEST_ERROR",
        category="system",
        retryable=True,
        error=RuntimeError("boom"),
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TEST_ERROR"
    assert payload["error"]["category"] == "system"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["message"] == "boom"
    assert "message" not in payload


def test_build_error_payload_with_top_level_message_and_extra() -> None:
    payload = build_error_payload(
        code="TEST_ERROR",
        category="storage",
        retryable=False,
        error="write failed",
        include_message_field=True,
        count=0,
    )
    assert payload["ok"] is False
    assert payload["message"] == "write failed"
    assert payload["error"]["retryable"] is False
    assert payload["count"] == 0


def test_build_stream_status_payload_shape() -> None:
    payload = build_stream_status_payload(
        status_type="chart_status",
        source="ibkr:db-cache-poll",
        code="CHART_STREAM_POLL_ERROR",
        category="stream",
        retryable=True,
        error=RuntimeError("poll failed"),
        retry_in_seconds=0.5,
        symbol="ES",
        interval="5m",
    )
    assert payload["type"] == "chart_status"
    assert payload["ok"] is False
    assert payload["message"] == "poll failed"
    assert payload["error"]["code"] == "CHART_STREAM_POLL_ERROR"
    assert payload["error"]["retryable"] is True
    assert payload["retry_in_seconds"] == 0.5
    assert payload["symbol"] == "ES"
    assert payload["interval"] == "5m"
    assert payload["ts"]


def test_build_action_error_response_shape() -> None:
    payload = build_action_error_response(
        code="PAPER_TRADES_LOAD_FAILED",
        category="trading",
        retryable=True,
        error=RuntimeError("db down"),
        trades=[],
    )
    assert payload["ok"] is False
    assert payload["message"] == "db down"
    assert payload["error"]["code"] == "PAPER_TRADES_LOAD_FAILED"
    assert payload["error"]["category"] == "trading"
    assert payload["trades"] == []
