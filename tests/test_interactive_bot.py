from __future__ import annotations

import inspect
import json
import threading
import time
from typing import Any

from aef_terminal.alerts.interactive_bot import TelegramInteractiveBot
from aef_terminal.alerts.snapshot_rendering import (
    _PngCanvas,
    _gex_history,
    _gex_heat_color,
    gex_market_data_status_text,
    render_gex_interval_png,
)
from aef_terminal.alerts.telegram import telegram_callback_token
from aef_terminal.config import AppConfig


def _watchlist_instrument(instrument_id: str, display: str) -> dict[str, str]:
    return {"instrument_id": instrument_id, "display": display}


def _snapshot_gex_level(
    price: float,
    *,
    spot: float,
    kind: str = "CALL_WALL",
    selection_rank: int = 1,
    call_gex: float = 150_000.0,
    put_gex: float = 0.0,
    peak_abs_gex: float | None = None,
) -> dict[str, Any]:
    abs_gex = abs(call_gex) + abs(put_gex)
    peak = abs_gex if peak_abs_gex is None else peak_abs_gex
    strength = round(abs_gex / peak, 4)
    power_class = (
        "WEAK"
        if strength < 0.10
        else "EXTREME"
        if strength >= 0.85
        else "STRONG"
        if strength >= 0.55
        else "MEDIUM"
    )
    return {
        "price": price,
        "kind": kind,
        "kind_class": "call" if kind == "CALL_WALL" else "put",
        "strength": strength,
        "power_class": power_class,
        "selection_rank": selection_rank,
        "net_gex": call_gex + put_gex,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "abs_gex": abs_gex,
        "abs_flow_1pt": abs_gex / (spot * 0.01),
        "distance_from_spot": price - spot,
        "zone_half_width": 0.25,
        "spot_side": "above" if price > spot else "below" if price < spot else "inside",
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


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request, **_kwargs):
        self.requests.append(request)
        return FakeResponse({"ok": True})

    def sent_payloads(self) -> list[dict[str, Any]]:
        payloads = []
        for request in self.requests:
            data = getattr(request, "data", None)
            if data:
                try:
                    payloads.append(json.loads(data.decode("utf-8")))
                except json.JSONDecodeError:
                    continue
        return payloads

    def sent_documents(self) -> list[Any]:
        return [
            request
            for request in self.requests
            if str(getattr(request, "full_url", "")).endswith("/sendDocument")
        ]

    def sent_photos(self) -> list[Any]:
        return [
            request
            for request in self.requests
            if str(getattr(request, "full_url", "")).endswith("/sendPhoto")
        ]


def test_interactive_bot_rejects_unauthorized_chat() -> None:
    opener = FakeOpener()
    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "999"}, "text": "/status"}})

    assert opener.requests == []


def test_interactive_bot_status_combines_system_server_database_and_ibkr() -> None:
    opener = FakeOpener()

    def system_status():
        return {
            "status": "ok",
            "checked_at": "2026-05-31T05:00:01+00:00",
            "app": {"pid": 123, "uptime_seconds": 60, "rss_bytes": 1024},
            "sleep": {"sleeping": False},
            "storage": {
                "ok": True,
                "bars": 1234,
                "latest_bar_ts": "2026-05-31T05:00:00+00:00",
                "database_bytes": 1024 * 1024 * 512,
                "providers": [
                    {"provider": "ibkr", "bars": 1000, "to": "2026-05-31T05:00:00+00:00"},
                ],
                "coverage": [
                    {
                        "provider": "ibkr",
                        "symbol": "ES",
                        "timeframe": "5m",
                        "bars": 600,
                        "to": "2026-05-31T05:00:00+00:00",
                    },
                ],
            },
            "ibkr": {
                "ok": True,
                "quote_subscriptions": 4,
                "quote_values": 3,
                "chart_streams": 2,
            },
        }

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        system_status_provider=system_status,
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/status"}})

    payload = opener.sent_payloads()[0]
    assert payload["chat_id"] == "123"
    assert "MartinCall Status" in payload["text"]
    assert "System:" in payload["text"]
    assert "Server" in payload["text"]
    assert "Database" in payload["text"]
    assert "Bars: 1,234" in payload["text"]
    assert "ibkr: 1,000" in payload["text"]
    assert "ES 5m ibkr: 600" in payload["text"]
    assert "IBKR" in payload["text"]
    assert "Quote subscriptions: 4" in payload["text"]


def test_interactive_bot_quote_command_uses_cached_quote_provider() -> None:
    opener = FakeOpener()

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        quote_provider=lambda instrument_id: {
            "instrument_id": instrument_id,
            "symbol": "ES",
            "price": 5012.25,
            "bid": 5012.0,
            "ask": 5012.5,
            "ts": "2026-05-31T05:00:00+00:00",
        },
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/quote instrument-es"}})

    payload = opener.sent_payloads()[0]
    assert "💎 <b>ES</b>" in payload["text"]
    assert "<b>Price: 5,012.25</b> | Bid: <b>5,012.00</b> | Ask: <b>5,012.50</b>" in payload["text"]


def test_interactive_bot_never_uses_opaque_instrument_id_as_display_fallback() -> None:
    source = inspect.getsource(TelegramInteractiveBot)

    assert 'or instrument_id or "unknown"' not in source


def test_interactive_bot_gexmaps_command_sends_cached_png_photos() -> None:
    opener = FakeOpener()
    captured_at = "2026-06-02T12:00:00+00:00"
    payload = {
        "ok": True,
        "instrument_id": "instrument-qqq",
        "status": "ok",
        "provider_symbol": "QQQ",
        "captured_at": captured_at,
        "capture_mode": "request",
        "market_data_entitlement": "delayed",
        "decision_authoritative": False,
        "spot": 756.0,
        "gamma_flip": None,
        "levels": [
            _snapshot_gex_level(
                755.0,
                spot=756.0,
                kind="PUT_WALL",
                selection_rank=2,
                call_gex=0.0,
                put_gex=-120_000.0,
                peak_abs_gex=150_000.0,
            ),
            _snapshot_gex_level(
                760.0,
                spot=756.0,
                selection_rank=1,
                peak_abs_gex=150_000.0,
            ),
        ],
        "history": [
            {
                "captured_at": captured_at,
                "timestamp_unix_ms": 1_780_405_200_000,
                "spot": 756.0,
                "source": "gex:ibkr",
                "capture_mode": "request",
                "market_data_entitlement": "delayed",
                "levels": [
                    _snapshot_gex_level(
                        755.0,
                        spot=756.0,
                        kind="PUT_WALL",
                        selection_rank=2,
                        call_gex=0.0,
                        put_gex=-120_000.0,
                        peak_abs_gex=150_000.0,
                    ),
                    _snapshot_gex_level(
                        760.0,
                        spot=756.0,
                        selection_rank=1,
                        peak_abs_gex=150_000.0,
                    ),
                ],
            }
        ],
    }

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        gex_provider=lambda _instrument_id: payload | {"symbol": "QQQ"},
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/gexmaps instrument-qqq"}})

    photos = opener.sent_photos()
    assert len(photos) == 2
    multipart = b"\n".join(getattr(request, "data", b"") for request in photos)
    assert b"gex-qqq-interval.png" in multipart
    assert b"gex-qqq-strike.png" in multipart
    assert multipart.count(b"\x89PNG\r\n\x1a\n") == 2
    assert b"QQQ GEX interval map" in multipart
    assert b"QQQ" in multipart
    assert b"Market data: DELAYED" in multipart
    assert b"DISPLAY ONLY" in multipart


def test_gex_snapshot_heatmap_keeps_zero_net_gamma_neutral() -> None:
    assert _gex_heat_color(0.0, 1.0, include_alpha=False) == "#64748b"
    assert _gex_heat_color(1.0, 1.0, include_alpha=False) == "#22c55e"
    assert _gex_heat_color(-1.0, 1.0, include_alpha=False) == "#22d3ee"


def test_gex_snapshot_history_renders_only_selected_capture_and_entitlement_lane() -> None:
    timestamp_ms = 1_780_405_200_000
    history = [
        {
            "timestamp_unix_ms": timestamp_ms + index * 60_000,
            "spot": 756.0,
            "source": "gex:ibkr",
            "capture_mode": "request",
            "market_data_entitlement": "live",
            "levels": [_snapshot_gex_level(760.0, spot=756.0)],
        }
        for index in range(150)
    ]
    history.insert(
        1,
        {
            "timestamp_unix_ms": timestamp_ms,
            "spot": 756.0,
            "source": "gex:ibkr-live",
            "capture_mode": "live",
            "market_data_entitlement": "live",
            "levels": [
                _snapshot_gex_level(
                    755.0,
                    spot=756.0,
                    kind="PUT_WALL",
                    call_gex=0.0,
                    put_gex=-120_000.0,
                )
            ],
        },
    )
    history.insert(
        2,
        {
            "timestamp_unix_ms": timestamp_ms,
            "spot": 756.0,
            "source": "gex:ibkr",
            "capture_mode": "request",
            "market_data_entitlement": "delayed",
            "levels": [_snapshot_gex_level(760.0, spot=756.0)],
        },
    )

    projected = _gex_history(
        {
            "spot": 756.0,
            "capture_mode": "request",
            "market_data_entitlement": "live",
            "history": history,
        }
    )

    assert len(projected) == 150
    assert {row["capture_mode"] for row in projected} == {"request"}
    assert {row["market_data_entitlement"] for row in projected} == {"live"}
    assert projected[0]["levels"][0]["strength"] == 1.0
    assert (
        _gex_history(
            {
                "captured_at": "2026-06-02T12:00:00+00:00",
                "levels": [{"strike": 760.0, "type": "CALL_WALL"}],
            }
        )
        == []
    )


def test_gex_interval_renderer_draws_canonical_strength_without_recomputation(monkeypatch) -> None:
    circles: list[tuple[float, float]] = []
    original_circle = _PngCanvas.circle

    def record_circle(self, cx, cy, radius, color, alpha=1.0):
        circles.append((radius, alpha))
        return original_circle(self, cx, cy, radius, color, alpha)

    monkeypatch.setattr(_PngCanvas, "circle", record_circle)
    render_gex_interval_png(
        {
            "spot": 756.0,
            "history": [
                {
                    "timestamp_unix_ms": 1_780_405_200_000,
                    "spot": 756.0,
                    "capture_mode": "request",
                    "market_data_entitlement": "live",
                    "levels": [_snapshot_gex_level(760.0, spot=756.0)],
                }
            ],
            "capture_mode": "request",
            "market_data_entitlement": "live",
            "decision_authoritative": True,
        },
        width=320,
        height=240,
    )

    assert circles[0][0] == 9.5
    assert round(circles[0][1], 2) == 0.92


def test_gex_market_data_status_never_promotes_nonlive_or_unknown_data() -> None:
    assert (
        gex_market_data_status_text(
            {"market_data_entitlement": "live", "decision_authoritative": True}
        )
        == "REALTIME · DECISION-AUTHORITATIVE"
    )
    assert (
        gex_market_data_status_text(
            {"market_data_entitlement": "frozen", "decision_authoritative": True}
        )
        == "FROZEN · DISPLAY ONLY"
    )
    assert gex_market_data_status_text({}) == "ENTITLEMENT UNKNOWN · DISPLAY ONLY"


def test_interactive_bot_chart_command_without_instrument_id_shows_chart_buttons() -> None:
    opener = FakeOpener()
    instruments = (
        ("instrument-es", "ES"),
        ("instrument-spy", "SPY"),
        ("instrument-qqq", "QQQ"),
        ("instrument-btc", "BTC"),
        ("instrument-eur-usd", "EUR.USD"),
    )
    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        watchlist_instruments_provider=lambda: [
            _watchlist_instrument(instrument_id, display) for instrument_id, display in instruments
        ],
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/chart"}})

    payload = opener.sent_payloads()[0]
    assert "Charts" in payload["text"]
    assert "Watchlist: ES, SPY, QQQ, BTC, EUR.USD" in payload["text"]
    assert "📈 Price: price frame with every visible object" in payload["text"]
    assert "🖥 Terminal: current terminal window with watchlist and volume" in payload["text"]
    assert "Requires an open terminal tab on the exact instrument." in payload["text"]
    rows = payload["reply_markup"]["inline_keyboard"]
    assert all(len(row) == 2 for row in rows)
    callback_names = {button["callback_data"] for row in rows for button in row}
    expected_callbacks = {
        f"cb_chart_{scope}_{telegram_callback_token(instrument_id, namespace='instrument')}"
        for instrument_id, _display in instruments
        for scope in ("p", "t")
    }
    assert callback_names == expected_callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_names)
    assert all(
        instrument_id not in callback
        for instrument_id, _display in instruments
        for callback in callback_names
    )
    assert opener.sent_photos() == []


def test_interactive_bot_dashboard_puts_alerts_charts_and_gex_first() -> None:
    opener = FakeOpener()
    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/help"}})

    payload = opener.sent_payloads()[0]
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "al"
    assert rows[1][0]["callback_data"] == "cb_chart"
    assert rows[2][0]["callback_data"] == "cb_gex"
    assert rows[3] == [{"text": "Status", "callback_data": "cb_status"}]
    assert {button["callback_data"] for row in rows for button in row}.isdisjoint(
        {"cb_system", "cb_db", "cb_ibkr"}
    )
    assert payload["text"].index("/alerts") < payload["text"].index("/chart")
    assert payload["text"].index("/gex") < payload["text"].index("/status")
    assert all(command not in payload["text"] for command in ("/system", "/db", "/ibkr"))


def test_interactive_bot_alert_manager_lists_and_rearms_by_compact_token() -> None:
    opener = FakeOpener()
    calls: list[tuple[str, dict[str, Any]]] = []

    def alert_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, dict(payload)))
        if action == "list":
            return {
                "ok": True,
                "counts": {"active": 0, "fired": 1, "disabled": 0},
                "alerts": [
                    {
                        "alert_type": "price",
                        "alert_id": "price-alert-1",
                        "callback_token": "alcompacttoken",
                        "symbol": "ES",
                        "timeframe": "5m",
                        "direction": "above",
                        "price": 5680.0,
                        "enabled": True,
                        "armed": False,
                        "fired": True,
                    }
                ],
            }
        if action == "rearm":
            return {
                "ok": True,
                "alert": {"symbol": "ES", "timeframe": "5m", "price": 5680.0},
            }
        return {"ok": False, "message": "unexpected"}

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        alert_command_provider=alert_command,
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/alerts"}})
    manager = opener.sent_payloads()[0]
    callbacks = {
        button["callback_data"]
        for row in manager["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert "al:a:r:p:alcompacttoken" in callbacks
    assert "ES" in manager["text"]

    bot.handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "al:a:r:p:alcompacttoken",
                "message": {"message_id": 77, "chat": {"id": "123"}},
            }
        }
    )

    assert (
        "rearm",
        {"callback_token": "alcompacttoken", "alert_type": "price"},
    ) in calls
    assert any(
        str(getattr(request, "full_url", "")).endswith("/editMessageReplyMarkup")
        for request in opener.requests
    )


def test_interactive_bot_serializes_alert_mutations_in_update_order() -> None:
    opener = FakeOpener()
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def alert_command(action: str, _payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(action)
        if action == "rearm":
            entered.set()
            release.wait(timeout=2.0)
        return {
            "ok": True,
            "alert": {"symbol": "ES", "timeframe": "5m", "price": 5680.0},
        }

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        alert_command_provider=alert_command,
        opener=opener,
    )
    rearm = {
        "callback_query": {
            "id": "callback-rearm",
            "data": "al:a:r:p:alcompacttoken",
            "message": {"message_id": 77, "chat": {"id": "123"}},
        }
    }
    disable = {
        "callback_query": {
            "id": "callback-disable",
            "data": "al:a:d:p:alcompacttoken",
            "message": {"message_id": 77, "chat": {"id": "123"}},
        }
    }

    first = bot.submit_update(rearm)
    assert entered.wait(timeout=1.0)
    second = bot.submit_update(disable)
    try:
        time.sleep(0.05)
        assert calls == ["rearm"]
        release.set()
        first.result(timeout=2.0)
        second.result(timeout=2.0)
        assert calls == ["rearm", "disable"]
    finally:
        release.set()
        bot.stop()


def test_interactive_bot_creates_fixed_price_alert_without_symbol_resolution() -> None:
    opener = FakeOpener()
    calls: list[tuple[str, dict[str, Any]]] = []

    def alert_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, dict(payload)))
        return {
            "ok": True,
            "alert": {
                "symbol": "QQQ",
                "timeframe": payload.get("timeframe"),
                "price": payload.get("price"),
            },
        }

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        alert_command_provider=alert_command,
        opener=opener,
    )

    bot.handle_update(
        {
            "message": {
                "chat": {"id": "123"},
                "text": "/alert instrument-qqq 5680 up 15m",
            }
        }
    )

    assert calls == [
        (
            "create_price",
            {
                "instrument_id": "instrument-qqq",
                "price": 5680.0,
                "direction": "above",
                "timeframe": "15m",
            },
        )
    ]
    assert "Price alert armed" in opener.sent_payloads()[0]["text"]


def test_interactive_bot_pins_direct_gex_alert_to_snapshot_version() -> None:
    opener = FakeOpener()
    calls: list[tuple[str, dict[str, Any]]] = []

    def alert_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, dict(payload)))
        if action == "gex_levels":
            return {
                "ok": True,
                "snapshot_version": "gs9ab12cd34ef5678",
                "levels": [{"selector": "CALL_WALL", "price": 5680.0}],
            }
        if action == "create_gex":
            return {
                "ok": True,
                "alert": {"symbol": "ES", "timeframe": "5m", "price": 5680.0},
            }
        return {"ok": False, "message": "unexpected"}

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        alert_command_provider=alert_command,
        opener=opener,
    )

    bot.handle_update(
        {
            "message": {
                "chat": {"id": "123"},
                "text": "/gexalert instrument-es CALL_WALL up 5m",
            }
        }
    )

    assert calls == [
        ("gex_levels", {"instrument_id": "instrument-es"}),
        (
            "create_gex",
            {
                "instrument_id": "instrument-es",
                "selector": "CALL_WALL",
                "direction": "above",
                "timeframe": "5m",
                "snapshot_version": "gs9ab12cd34ef5678",
            },
        ),
    ]
    assert "GEX snapshot alert armed" in opener.sent_payloads()[0]["text"]


def test_interactive_bot_carries_compact_gex_snapshot_version_through_callbacks() -> None:
    opener = FakeOpener()
    calls: list[tuple[str, dict[str, Any]]] = []
    snapshot_version = "gs9ab12cd34ef5678"
    instrument_id = "instrument-es"
    instrument_token = telegram_callback_token(instrument_id, namespace="instrument")

    def alert_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, dict(payload)))
        if action == "gex_levels":
            return {
                "ok": True,
                "captured_at": "2026-07-21T12:00:00.123456+00:00",
                "snapshot_version": snapshot_version,
                "levels": [{"selector": "CALL_WALL", "label": "CALL WALL", "price": 5680.0}],
            }
        return {
            "ok": True,
            "alert": {"symbol": "ES", "timeframe": "5m", "price": 5680.0},
        }

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        watchlist_instruments_provider=lambda: [_watchlist_instrument(instrument_id, "ES")],
        alert_command_provider=alert_command,
        opener=opener,
    )

    def callback(data: str, callback_id: str) -> None:
        bot.handle_update(
            {
                "callback_query": {
                    "id": callback_id,
                    "data": data,
                    "message": {"message_id": 77, "chat": {"id": "123"}},
                }
            }
        )

    callback(f"al:g:{instrument_token}", "gex-levels")
    messages = [payload for payload in opener.sent_payloads() if "text" in payload]
    level_callback = messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert level_callback == f"al:gl:{instrument_token}:{snapshot_version}:CALL_WALL"

    callback(level_callback, "gex-direction")
    messages = [payload for payload in opener.sent_payloads() if "text" in payload]
    direction_callbacks = [
        button["callback_data"] for button in messages[-1]["reply_markup"]["inline_keyboard"][0]
    ]
    assert all(len(value.encode("utf-8")) <= 64 for value in direction_callbacks)
    above_callback = next(value for value in direction_callbacks if value.endswith(":above"))

    callback(above_callback, "gex-create")
    assert (
        "create_gex",
        {
            "instrument_id": instrument_id,
            "snapshot_version": snapshot_version,
            "selector": "CALL_WALL",
            "direction": "above",
            "timeframe": "5m",
        },
    ) in calls


def test_interactive_bot_chart_buttons_request_browser_png_for_both_scopes() -> None:
    opener = FakeOpener()
    instrument_id = "instrument-spy"
    instrument_token = telegram_callback_token(
        instrument_id,
        namespace="instrument",
    )
    capture_calls: list[tuple[str, str]] = []

    def browser_capture(request_instrument_id: str, scope: str) -> dict[str, Any]:
        capture_calls.append((request_instrument_id, scope))
        return {
            "ok": True,
            "instrument_id": request_instrument_id,
            "scope": scope,
            "timeframe": "5m",
            "captured_at": "2026-06-02T12:10:00+00:00",
            "content": b"\x89PNG\r\n\x1a\n" + scope.encode("ascii"),
        }

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        browser_capture_provider=browser_capture,
        watchlist_instruments_provider=lambda: [_watchlist_instrument(instrument_id, "SPY")],
        opener=opener,
    )

    for suffix, callback_id in (("p", "cb-price"), ("t", "cb-terminal")):
        callback_data = f"cb_chart_{suffix}_{instrument_token}"
        assert len(callback_data.encode("utf-8")) <= 64
        bot.handle_update(
            {
                "callback_query": {
                    "id": callback_id,
                    "data": callback_data,
                    "message": {"chat": {"id": "123"}},
                }
            }
        )

    photos = opener.sent_photos()
    assert capture_calls == [
        (instrument_id, "price"),
        (instrument_id, "terminal"),
    ]
    assert len(photos) == 2
    price_multipart = getattr(photos[0], "data", b"")
    terminal_multipart = getattr(photos[1], "data", b"")
    assert b"chart-price.png" in price_multipart
    assert b"SPY price" in price_multipart
    assert b"Browser tab" in price_multipart
    assert b"PRICE" in price_multipart
    assert b"chart-terminal.png" in terminal_multipart
    assert b"SPY terminal" in terminal_multipart
    assert b"Browser tab" in terminal_multipart
    assert b"TERMINAL" in terminal_multipart


def test_interactive_bot_typed_browser_capture_failure_stays_failed_closed() -> None:
    opener = FakeOpener()

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        browser_capture_provider=lambda instrument_id, scope: {
            "ok": False,
            "instrument_id": instrument_id,
            "scope": scope,
            "route_fingerprint": "route:exact",
            "error_code": "BROWSER_CAPTURE_TIMEOUT",
            "elapsed_ms": 15_000,
        },
        opener=opener,
    )

    bot.send_chart_snapshot("123", "instrument-es", capture_scope="terminal")

    assert opener.sent_photos() == []
    assert "BROWSER_CAPTURE_TIMEOUT" in opener.sent_payloads()[-1]["text"]
    assert "Open the exact instrument" in opener.sent_payloads()[-1]["text"]


def test_interactive_bot_gex_status_includes_map_buttons() -> None:
    opener = FakeOpener()
    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        system_status_provider=lambda: {
            "gex": {
                "ok": True,
                "scheduler": {
                    "enabled": True,
                    "instrument_ids": ["instrument-es", "instrument-spy", "instrument-qqq"],
                },
            }
        },
        watchlist_instruments_provider=lambda: [
            _watchlist_instrument(instrument_id, display)
            for instrument_id, display in (
                ("instrument-es", "ES"),
                ("instrument-spy", "SPY"),
                ("instrument-qqq", "QQQ"),
            )
        ],
        opener=opener,
    )

    bot.handle_update({"message": {"chat": {"id": "123"}, "text": "/gex"}})

    payload = opener.sent_payloads()[0]
    markup = payload["reply_markup"]["inline_keyboard"]
    callback_names = {button["callback_data"] for row in markup for button in row}
    assert {"cb_gex_map_instrument-es", "cb_gex_strike_instrument-qqq"}.issubset(callback_names)


def test_interactive_bot_poll_does_not_block_on_slow_command() -> None:
    release = threading.Event()
    entered = threading.Event()

    class PollOpener(FakeOpener):
        def __call__(self, request, **_kwargs):
            self.requests.append(request)
            if isinstance(request, str):
                return FakeResponse(
                    {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 41,
                                "message": {"chat": {"id": "123"}, "text": "/chart instrument-es"},
                            }
                        ],
                    }
                )
            return FakeResponse({"ok": True})

    def slow_browser_capture(instrument_id: str, scope: str) -> dict[str, Any]:
        entered.set()
        release.wait(timeout=2.0)
        return {
            "ok": False,
            "instrument_id": instrument_id,
            "scope": scope,
            "error_code": "BROWSER_CAPTURE_TIMEOUT",
        }

    opener = PollOpener()
    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
            telegram_interactive_poll_timeout_seconds=1.0,
        ),
        browser_capture_provider=slow_browser_capture,
        opener=opener,
    )

    started = time.monotonic()
    bot.poll_once()
    elapsed = time.monotonic() - started

    try:
        assert elapsed < 0.25
        assert bot.last_update_id == 41
        assert entered.wait(timeout=1.0)
        assert bot.status()["inflight_commands"] >= 1
    finally:
        release.set()
        deadline = time.monotonic() + 2.0
        while bot.status()["inflight_commands"] and time.monotonic() < deadline:
            time.sleep(0.01)
        bot.stop()


def test_interactive_bot_stop_waits_for_running_command() -> None:
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, Any] = {}

    def system_status() -> dict[str, Any]:
        entered.set()
        assert release.wait(timeout=2.0)
        return {}

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        system_status_provider=system_status,
        opener=FakeOpener(),
    )
    command = bot.submit_update({"message": {"chat": {"id": "123"}, "text": "/status"}})
    assert entered.wait(timeout=1.0)

    def stop() -> None:
        outcome.update(bot.stop(timeout=0.1))

    stopper = threading.Thread(target=stop)
    stopper.start()
    time.sleep(0.05)
    assert stopper.is_alive()
    assert command.done() is False
    release.set()
    stopper.join(timeout=2.0)

    assert stopper.is_alive() is False
    assert command.done() is True
    assert outcome == {
        "drained": True,
        "thread_alive": False,
        "inflight_commands": 0,
    }


def test_interactive_bot_stop_reports_undrained_command_without_losing_owner() -> None:
    entered = threading.Event()
    release = threading.Event()

    def system_status() -> dict[str, Any]:
        entered.set()
        assert release.wait(timeout=2.0)
        return {}

    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="123",
        ),
        system_status_provider=system_status,
        opener=FakeOpener(),
    )
    command = bot.submit_update({"message": {"chat": {"id": "123"}, "text": "/status"}})
    assert entered.wait(timeout=1.0)

    first = bot.stop(timeout=0.05)

    assert first == {
        "drained": False,
        "thread_alive": False,
        "inflight_commands": 1,
    }
    assert command.done() is False
    release.set()
    command.result(timeout=2.0)
    assert bot.stop(timeout=0.1) == {
        "drained": True,
        "thread_alive": False,
        "inflight_commands": 0,
    }


def test_interactive_bot_force_start_uses_server_setting_without_env_flag() -> None:
    bot = TelegramInteractiveBot(
        AppConfig(
            telegram_interactive_enabled=False,
            telegram_bot_token="token",
            telegram_chat_id="123",
        )
    )
    started = {"value": False}

    def fake_start():
        started["value"] = True

    bot._thread = None
    bot.run_forever = fake_start

    assert bot.enabled() is False
    assert bot.start_background(force=True) is True
    assert bot._thread is not None
