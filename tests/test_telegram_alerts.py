import time
import json

from aef_terminal.alerts.telegram import (
    format_alert_message,
    format_paper_trade_message,
    send_telegram_message,
    telegram_config_status,
)
from aef_terminal.config import AppConfig
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui import telegram_runtime as ui_telegram_runtime
from tests.provider_payloads import ibkr_future_payload


def test_telegram_status_masks_chat_id() -> None:
    status = telegram_config_status(
        AppConfig(
            telegram_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123456789",
        )
    )

    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["chat_id"] == "12***89"


def test_telegram_interactive_quote_never_reads_durable_snapshot_fallback(monkeypatch) -> None:
    instrument = ibkr_future_payload("CL")
    route = route_instrument(instrument)
    store_requested = False

    def forbidden_store():
        nonlocal store_requested
        store_requested = True
        raise AssertionError("interactive quote must not query PostgreSQL")

    monkeypatch.setattr(
        ui_telegram_runtime,
        "_DEPS",
        ui_telegram_runtime.TelegramRuntimeDeps(
            store_factory=forbidden_store,
            selected_instruments=lambda: [instrument],
            quote_cache_for_instruments=lambda _instruments: (
                {route.fingerprint: {"bid": 90.0, "ask": 90.1}},
                "quote unavailable",
                object(),
            ),
        ),
    )
    monkeypatch.setattr(
        ui_telegram_runtime,
        "lookup_runtime_instrument",
        lambda instrument_id: instrument if instrument_id == route.instrument_id else None,
    )

    result = ui_telegram_runtime.telegram_interactive_quote(route.instrument_id)

    assert result["ok"] is False
    assert result["source"] == "ibkr:quote-cache"
    assert result["message"] == "quote unavailable"
    assert store_requested is False


def test_format_alert_message_includes_symbol_level_plan_and_local_time(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    if hasattr(time, "tzset"):
        time.tzset()
    message = format_alert_message(
        {
            "symbol": "ES",
            "timeframe": "5m",
            "alert_type": "price",
            "kind": "Price alert",
            "direction": "above",
            "price": 7542.5,
            "current_price": 7543.0,
            "trigger": 7542.5,
            "stop": 7539.25,
            "target": 7548.75,
            "source": "test",
            "ts": "2026-05-26T06:00:00Z",
        }
    )

    assert message == "\n".join(
        [
            "📈 <a href='https://www.tradingview.com/chart/?symbol=ES'><b>ES</b></a> <b>[5m]</b> | <code>09:00 2026-05-26</code>",
            "📍 Moved above level: <b>7542.50</b> -> <b>7543.00</b> | <b>E 7542.50</b> / <b>S 7539.25</b> / <b>T 7548.75</b>",
        ]
    )


def test_format_alert_message_price_alert_is_compact_local_time(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    if hasattr(time, "tzset"):
        time.tzset()

    message = format_alert_message(
        {
            "symbol": "QQQ",
            "timeframe": "5m",
            "kind": "Price alert",
            "direction": "cross",
            "price": 746.97,
            "current_price": 747.10,
            "source": "MartinCall backend",
            "ts": "2026-06-02T20:05:06.347598+00:00",
            "message": "fired",
        }
    )

    assert message == "\n".join(
        [
            "🔔 <a href='https://www.tradingview.com/chart/?symbol=QQQ'><b>QQQ</b></a> <b>[5m]</b> | <code>23:05 2026-06-02</code>",
            "📍 Level crossed: <b>746.97</b> -> <b>747.10</b>",
        ]
    )
    assert "MartinCall" not in message
    assert "backend" not in message
    assert "fired" not in message


def test_send_telegram_message_disabled_does_not_require_network() -> None:
    result = send_telegram_message(
        "hello",
        config=AppConfig(
            telegram_enabled=False,
            telegram_bot_token="token",
            telegram_chat_id="123456789",
        ),
    )

    assert result["ok"] is False
    assert result["status"] == "disabled"


def test_send_server_telegram_alert_sends_when_env_disabled_but_configured(monkeypatch) -> None:
    sent = []
    ui_telegram_runtime.TELEGRAM_ALERT_DEDUP.clear()
    monkeypatch.setattr(
        ui_telegram_runtime,
        "send_telegram_message",
        lambda text, force=False, reply_markup=None: (
            sent.append({"text": text, "force": force, "reply_markup": reply_markup})
            or {"ok": True, "status": "sent"}
        ),
    )
    route = route_instrument(ibkr_future_payload("CL"))
    result = ui_telegram_runtime.send_server_telegram_alert(
        {
            "alert_id": "alert-disabled-env",
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "symbol": "CL",
            "timeframe": "5m",
            "kind": "Price alert",
            "direction": "cross",
            "price": 90.87,
            "current_price": 90.82,
            "ts": "2026-06-05T15:30:00+00:00",
        }
    )
    assert result["status"] == "sent"
    assert len(sent) == 1
    assert sent[0]["force"] is True
    callbacks = {
        button["callback_data"]
        for row in sent[0]["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert any(callback.startswith("al:a:r:p:") for callback in callbacks)
    assert any(callback.startswith("al:a:c:p:") for callback in callbacks)
    assert any(callback.startswith("al:a:d:p:") for callback in callbacks)
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)


def test_send_telegram_message_can_be_silent_without_changing_alert_default(monkeypatch) -> None:
    bodies = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    config = AppConfig(telegram_enabled=True, telegram_bot_token="token", telegram_chat_id="123")

    alert_result = send_telegram_message("alert", config=config)
    silent_result = send_telegram_message("paper", config=config, silent=True)

    assert alert_result["status"] == "sent"
    assert silent_result["status"] == "sent"
    assert "disable_notification" not in bodies[0]
    assert bodies[1]["disable_notification"] is True


def test_format_paper_trade_message_is_compact_and_trade_specific(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    if hasattr(time, "tzset"):
        time.tzset()

    message = format_paper_trade_message(
        {
            "symbol": "ES",
            "timeframe": "5m",
            "side": "long",
            "source": "linda_volume",
            "entry": 100.0,
            "stop": 98.0,
            "target": 103.0,
            "opened_at": "2026-06-05T10:00:00+00:00",
        },
        event="open",
    )

    assert message == "\n".join(
        [
            "🟢 <b>OPEN</b> <a href='https://www.tradingview.com/chart/?symbol=ES'><b>ES</b></a> <b>[5m]</b> <b>LONG</b> | <code>13:00 2026-06-05</code>",
            "📝 Plan: E <b><code>100.00</code></b> / S <b><code>98.00</code></b> / T <b><code>103.00</code></b> | RR 1.50",
        ]
    )


def test_server_telegram_alert_dedupes_same_level_across_alert_ids(monkeypatch) -> None:
    sent = []
    ui_telegram_runtime.TELEGRAM_ALERT_DEDUP.clear()
    monkeypatch.setattr(
        ui_telegram_runtime,
        "send_telegram_message",
        lambda text, force=False, reply_markup=None: (
            sent.append({"text": text, "reply_markup": reply_markup})
            or {"ok": True, "status": "sent"}
        ),
    )
    route = route_instrument(ibkr_future_payload("CL"))
    base = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "CL",
        "timeframe": "5m",
        "kind": "Price alert",
        "direction": "cross",
        "price": 90.87,
        "source": "MartinCall backend",
        "message": "fired",
    }

    first = ui_telegram_runtime.send_server_telegram_alert(
        {**base, "alert_id": "alert-a", "current_price": 90.82, "ts": "2026-06-05T15:30:00+00:00"}
    )
    second = ui_telegram_runtime.send_server_telegram_alert(
        {**base, "alert_id": "alert-b", "current_price": 90.86, "ts": "2026-06-05T15:32:00+00:00"}
    )

    assert first["status"] == "sent"
    assert second["status"] == "duplicate"
    assert len(sent) == 1
    assert (
        "🔔 <a href='https://www.tradingview.com/chart/?symbol=CL'><b>CL</b></a> <b>[5m]</b>"
        in sent[0]["text"]
    )
    assert "📍 Level crossed: <b>90.87</b> -> <b>90.82</b>" in sent[0]["text"]


def test_server_telegram_alert_allows_new_trigger_generation_after_rearm(monkeypatch) -> None:
    sent = []
    ui_telegram_runtime.TELEGRAM_ALERT_DEDUP.clear()
    monkeypatch.setattr(
        ui_telegram_runtime,
        "send_telegram_message",
        lambda text, force=False, reply_markup=None: (
            sent.append(text) or {"ok": True, "status": "sent"}
        ),
    )
    route = route_instrument(ibkr_future_payload("CL"))
    payload = {
        "alert_id": "alert-a",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "CL",
        "timeframe": "5m",
        "kind": "Price alert",
        "direction": "cross",
        "price": 90.87,
        "current_price": 90.82,
    }

    first = ui_telegram_runtime.send_server_telegram_alert(
        {**payload, "trigger_event_at": 1784635200000}
    )
    retry = ui_telegram_runtime.send_server_telegram_alert(
        {**payload, "trigger_event_at": 1784635200000}
    )
    rearmed_event = ui_telegram_runtime.send_server_telegram_alert(
        {**payload, "trigger_event_at": 1784635500000}
    )

    assert first["status"] == "sent"
    assert retry["status"] == "duplicate"
    assert rearmed_event["status"] == "sent"
    assert len(sent) == 2


def test_server_telegram_alert_does_not_dedupe_failed_delivery(monkeypatch) -> None:
    calls = []
    ui_telegram_runtime.TELEGRAM_ALERT_DEDUP.clear()

    def fake_send(text, force=False, reply_markup=None):
        calls.append({"text": text, "force": force, "reply_markup": reply_markup})
        if len(calls) == 1:
            return {"ok": False, "status": "send_failed", "detail": "timed out"}
        return {"ok": True, "status": "sent"}

    monkeypatch.setattr(ui_telegram_runtime, "send_telegram_message", fake_send)
    route = route_instrument(ibkr_future_payload("CL"))
    payload = {
        "alert_id": "alert-a",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "CL",
        "timeframe": "5m",
        "kind": "Price alert",
        "direction": "cross",
        "price": 90.87,
        "current_price": 90.82,
        "ts": "2026-06-05T15:30:00+00:00",
    }

    first = ui_telegram_runtime.send_server_telegram_alert(payload)
    second = ui_telegram_runtime.send_server_telegram_alert(payload)
    third = ui_telegram_runtime.send_server_telegram_alert(payload)

    assert first["status"] == "send_failed"
    assert second["status"] == "sent"
    assert third["status"] == "duplicate"
    assert len(calls) == 2
