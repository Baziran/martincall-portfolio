import asyncio
import inspect
import json
import logging
import threading
from datetime import UTC, datetime

import pytest

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.alerts.runtime_registry import AlertRuntimeRegistry
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_sessions import ProviderBarSlotMap
from aef_terminal.ui.alert_runtime_rules import alert_monitor_payload
from aef_terminal.ui.route_selection import parse_route_selection
from aef_terminal.ui.quote_stream_contract import project_quote_stream_row
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision
from aef_terminal.runtime.option_target_changes import option_target_mutation
from aef_terminal.ui.services import quote_stream_ws
from aef_terminal.ui.services.quote_stream_ws import (
    _QUOTE_SNAPSHOT_PRODUCERS,
    QuoteStreamWsDeps,
    _QuoteSnapshotProducer,
    _release_quote_snapshot_producer,
    _run_quote_snapshot_producer,
    _update_quote_live_bars,
    run_quote_stream,
)
from tests.provider_payloads import ibkr_future_payload, quote_route_snapshot


TEST_QUOTE_CACHE_EPOCH = "00000000-0000-4000-8000-000000000001"


def quote_cache_revision(generation: int) -> QuoteCacheRevision:
    return QuoteCacheRevision(TEST_QUOTE_CACHE_EPOCH, generation)


def provider_identity(
    key: str,
    provider: str,
    provider_symbol: str | None = None,
    *,
    asset_class: str = "index",
) -> dict:
    route = provider_symbol or key
    contract_id = f"{provider}:{route}"
    payload = {
        "instrument_id": f"{provider}|contract|{contract_id}",
        "key": key,
        "instrument_key": key,
        "symbol": key,
        "display": key,
        "provider": provider,
        "provider_symbol": route,
        "provider_contract_id": contract_id,
        "asset_class": asset_class,
        "contract_identity": {
            "provider": provider,
            "provider_contract_id": contract_id,
            "asset_class": asset_class,
        },
    }
    resolved = route_instrument(payload)
    payload["route_fingerprint"] = resolved.fingerprint
    return payload


def route_query(*instruments: dict) -> str:
    return json.dumps(
        [
            {
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
            }
            for route in map(route_instrument, instruments)
        ]
    )


def quote_row(instrument: dict, **values) -> dict:
    route = route_instrument(instrument)
    quote_ts = "2026-07-16T14:45:00+00:00"
    return {
        "key": route.instrument_key,
        "display": instrument["display"],
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider_symbol": route.provider_symbol,
        "price": 100.0,
        "bid": 99.0,
        "ask": 101.0,
        "last": 100.0,
        "quote_close": None,
        "quote_ts": quote_ts,
        "price_source": "last",
        "quote_time_basis": "provider_event",
        "quote_provider_ts": quote_ts,
        "quote_received_at": quote_ts,
        "quote_status": "live",
        "quote_entitlement": "live",
        "quote_is_delayed": False,
        "quote_is_stale": False,
        "last_provider_ts": quote_ts,
        "last_status": "live",
        "bid_ask_received_at": quote_ts,
        "bid_ask_status": "live",
        "change": None,
        "change_pct": None,
        "previous_session_close": None,
        "change_base": None,
        "source": f"{route.provider}:quote-live",
        "live_quote": True,
        "warning": "",
        "contract": None,
        "local_symbol": None,
        "contract_month": None,
        "contract_rollover_due": False,
        "contract_rollover_warning": None,
        "contract_rollover_new": False,
        "contract_rollover_new_message": None,
        **values,
    }


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.messages = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload) -> None:
        self.messages.append(payload)


def quote_routes(*instruments):
    snapshot = quote_route_snapshot(list(instruments))
    return lambda: snapshot


def _deps(**overrides):
    defaults = {
        "logger": logging.getLogger(__name__),
        "store_factory": lambda: type(
            "Store",
            (),
            {
                "read_option_targets": lambda self, **_kwargs: [],
                "read_price_alerts": lambda self, **_kwargs: [],
                "read_bar_slots": lambda self, *_args, required_timestamps=(), **_kwargs: (
                    ProviderBarSlotMap(
                        {
                            timestamp: int(timestamp.timestamp() // 60)
                            for timestamp in required_timestamps
                        },
                        schedule_state="verified",
                    )
                ),
            },
        )(),
        "apply_provider_runtime_settings_async": lambda: asyncio.sleep(0, result={}),
        "server_sleeping": lambda: False,
        "quote_route_snapshot": quote_routes(provider_identity("RISK", "ibkr")),
        "set_quote_stream_wanted": lambda _key, _symbols: None,
        "quote_stream_started": lambda: 1,
        "quote_stream_finished": lambda: 0,
        "screener_bases": lambda _instruments, _interval: {},
        "screener_rows": lambda instruments, _bases, _live_map, warning, _interval: [
            quote_row(instrument, warning=warning) for instrument in instruments
        ],
        "quote_cache_for_instruments": (
            lambda _instruments, *, after_revision=None: (
                None if after_revision == quote_cache_revision(0) else {},
                "",
                quote_cache_revision(0),
            )
        ),
        "record_quote_execution_snapshots": lambda _rows: None,
        "fast_indicator_snapshots": lambda _targets, _interval: [],
        "quote_stream_seconds": 0.01,
        "quote_stream_cleanup_grace_seconds": 0.0,
        "websocket_heartbeat_seconds": 15.0,
        "stream_status_payload": lambda **kwargs: {
            "type": kwargs["status_type"],
            "error": kwargs["code"],
        },
    }
    defaults.update(overrides)
    return QuoteStreamWsDeps(**defaults)


def test_quote_stream_projection_omits_browser_unused_churn_without_mutating_row() -> None:
    instrument = provider_identity("SPY", "ibkr")
    row = quote_row(
        instrument,
        quote_received_at="2026-07-16T14:45:01+00:00",
        quote_age_seconds=1.234,
        price_source="bid_ask_mid",
        quote_time_basis="client_receive",
        quote_status="live",
        quote_is_stale=False,
        last_provider_ts="2026-07-16T14:44:40+00:00",
        last_status="stale",
        bid_ask_received_at="2026-07-16T14:45:01+00:00",
        bid_ask_status="live",
        trend_interval="5m",
        trend_points=[{"ts": "2026-07-16T14:45:00+00:00", "price": 100.0}],
        backend_only_revision=99,
        warning="",
    )

    projected = project_quote_stream_row(row)

    assert projected["quote_received_at"] == "2026-07-16T14:45:01+00:00"
    assert "quote_age_seconds" not in projected
    assert projected["price_source"] == "bid_ask_mid"
    assert projected["quote_time_basis"] == "client_receive"
    assert projected["quote_status"] == "live"
    assert projected["quote_is_stale"] is False
    assert projected["last_status"] == "stale"
    assert projected["bid_ask_status"] == "live"
    assert "trend_interval" not in projected
    assert "trend_points" not in projected
    assert "backend_only_revision" not in projected
    assert projected["instrument_id"] == row["instrument_id"]
    assert projected["route_fingerprint"] == row["route_fingerprint"]
    assert "quote_age_seconds" in row
    assert "trend_points" in row


def test_quote_stream_projection_rejects_partial_quote_frame() -> None:
    row = quote_row(provider_identity("SPY", "ibkr"))
    del row["quote_received_at"]

    with pytest.raises(ValueError, match="incomplete: quote_received_at"):
        project_quote_stream_row(row)


def test_quote_stream_projection_rejects_coercible_quote_geometry() -> None:
    row = quote_row(provider_identity("SPY", "ibkr"), price="100.0")

    with pytest.raises(ValueError, match="fields are malformed"):
        project_quote_stream_row(row)


def test_quote_stream_contract_classifies_every_field_and_rejects_malformed_rollover() -> None:
    from aef_terminal.ui.quote_stream_contract import quote_stream_row_contract_manifest

    manifest = quote_stream_row_contract_manifest()
    classifications = {field: [] for field in manifest["required_fields"]}
    for group in (
        "number_fields",
        "boolean_fields",
        "time_fields",
        "status_fields",
        "text_fields",
        "nullable_text_fields",
    ):
        for field in manifest[group]:
            classifications[field].append(group)
    for field in manifest["nullable_object_contracts"]:
        classifications[field].append("nullable_object_contracts")
    classifications[manifest["entitlement_field"]].append("entitlement_field")

    assert all(len(groups) == 1 for groups in classifications.values())
    malformed = quote_row(
        provider_identity("ES", "ibkr"),
        contract_rollover_warning={
            "status": "rollover_due",
            "days_left": "2",
            "expiry_date": "2026-09-18",
            "contract_month": "202609",
            "message": "Roll soon",
        },
    )
    with pytest.raises(ValueError, match="fields are malformed"):
        project_quote_stream_row(malformed)


def test_quote_stream_sends_one_full_snapshot_then_only_dirty_rows() -> None:
    first = provider_identity("SPY", "ibkr")
    second = provider_identity("QQQ", "ibkr")
    websocket = FakeWebSocket()
    row_calls = 0

    def rows(instruments, *_args):
        nonlocal row_calls
        row_calls += 1
        return [
            quote_row(instrument, price=101.0 if row_calls > 1 and index == 0 else 100.0)
            for index, instrument in enumerate(instruments)
        ]

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(first, second),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(first, second),
                    screener_rows=rows,
                    quote_cache_for_instruments=(
                        lambda _instruments, *, after_revision=None: (
                            None if after_revision == quote_cache_revision(row_calls) else {},
                            "",
                            quote_cache_revision(row_calls),
                        )
                    ),
                    server_sleeping=lambda: any(
                        message.get("type") == "quote_delta" for message in websocket.messages
                    ),
                ),
            ),
            timeout=1.0,
        )

    asyncio.run(scenario())

    snapshots = [
        message for message in websocket.messages if message.get("type") == "quote_snapshot"
    ]
    deltas = [message for message in websocket.messages if message.get("type") == "quote_delta"]
    assert snapshots[0]["sequence"] == 1
    assert snapshots[0]["cache_epoch"] == TEST_QUOTE_CACHE_EPOCH
    assert isinstance(snapshots[0]["cache_generation"], int)
    assert len(snapshots) == 1
    assert len(snapshots[0]["rows"]) == 2
    assert len(deltas) == 1
    assert deltas[0]["cache_epoch"] == TEST_QUOTE_CACHE_EPOCH
    assert deltas[0]["cache_generation"] >= snapshots[0]["cache_generation"]
    assert len(deltas[0]["rows"]) == 1
    assert deltas[0]["rows"][0]["instrument_id"] == route_instrument(first).instrument_id
    assert deltas[0]["rows"][0]["price"] == 101.0
    sequenced = [
        message["sequence"]
        for message in websocket.messages
        if message.get("type")
        in {
            "quote_snapshot",
            "quote_delta",
            "quote_heartbeat",
            "option_targets_snapshot",
            "fast_indicators_snapshot",
            "quote_aux_status",
        }
    ]
    assert sequenced == list(range(1, len(sequenced) + 1))


def test_quote_stream_reports_route_change_after_initial_snapshot() -> None:
    instrument = provider_identity("ES", "ibkr")
    websocket = FakeWebSocket()
    selection_calls = 0
    initial_routes = quote_route_snapshot([instrument])
    retired_routes = quote_route_snapshot([])

    def selected_instruments():
        nonlocal selection_calls
        selection_calls += 1
        return initial_routes if not websocket.messages else retired_routes

    def cache_read(_instruments, *, after_revision=None):
        generation = selection_calls
        return (
            None if after_revision == quote_cache_revision(generation) else {},
            "",
            quote_cache_revision(generation),
        )

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=selected_instruments,
                quote_cache_for_instruments=cache_read,
            ),
        )
    )

    snapshot_index = next(
        index
        for index, message in enumerate(websocket.messages)
        if message.get("type") == "quote_snapshot"
    )
    route_change_index = next(
        index
        for index, message in enumerate(websocket.messages)
        if message.get("error") == "QUOTE_STREAM_ROUTE_CHANGED"
    )
    assert route_change_index > snapshot_index


def test_quote_stream_publishes_fast_indicators_after_matching_option_targets() -> None:
    instrument = provider_identity("ES", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    target = {
        "id": "opt-fast",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "point": {"price": 100.0},
    }
    indicator = {
        "latest": {"state": "WATCH", "score": 70.0},
        "execution": {
            "lane": "fast",
            "authority": "advisory_only",
            "trigger": "option_target_sample",
        },
    }
    scope = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "bar_timeframe": "1m",
        "bar_canonical_generation": 7,
        "revision": 4,
        "updated_at": "2026-08-07T12:00:00+00:00",
        "authority": "advisory_only",
        "indicator_id": "option_reversal",
        "indicator": indicator,
        "by_target_id": {"opt-fast": indicator},
    }

    class Store:
        def read_option_targets(self, **_kwargs):
            return [{"payload": target}]

        def read_price_alerts(self, **_kwargs):
            return []

    def fast_snapshots(targets, interval):
        assert interval == "5m"
        return [scope] if targets == [target] else []

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                store_factory=Store,
                fast_indicator_snapshots=fast_snapshots,
                server_sleeping=lambda: any(
                    message.get("type") == "fast_indicators_snapshot"
                    for message in websocket.messages
                ),
            ),
        )
    )

    target_index = next(
        index
        for index, message in enumerate(websocket.messages)
        if message.get("type") == "option_targets_snapshot"
    )
    fast_index = next(
        index
        for index, message in enumerate(websocket.messages)
        if message.get("type") == "fast_indicators_snapshot"
    )
    assert target_index < fast_index
    assert websocket.messages[fast_index]["source"] == "quote:fast-indicators"
    assert websocket.messages[fast_index]["scopes"] == [scope]


def test_quote_fast_indicator_projection_clears_while_source_generation_is_behind() -> None:
    instrument = provider_identity("ES", "ibkr")
    route = route_instrument(instrument)
    target = {
        "id": "opt-fast",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
    }
    scope = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "bar_timeframe": "1m",
        "bar_canonical_generation": 7,
        "revision": 4,
        "updated_at": "2026-08-07T12:00:00+00:00",
        "authority": "advisory_only",
        "indicator_id": "option_reversal",
        "indicator": None,
        "by_target_id": {},
    }
    snapshots = iter(([scope], None))
    producer = _QuoteSnapshotProducer(
        key=("5m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(fast_indicator_snapshots=lambda _targets, _interval: next(snapshots)),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="5m",
    )
    producer.option_targets = [target]
    producer.targets_revision = 1
    runtime = quote_stream_ws._QuoteProducerRuntime(producer)

    runtime.refresh_fast_indicators(1.0)
    assert producer.fast_indicators == [scope]
    assert producer.fast_indicators_revision == 1

    runtime.refresh_fast_indicators(2.0)
    assert producer.fast_indicators == []
    assert producer.fast_indicators_revision == 2


def test_quote_stream_does_not_consume_revisions_advanced_while_send_yields() -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    original_send_json = websocket.send_json
    row_advanced = False
    target_advanced = False
    updated_target = {
        "id": "opt-raced",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "payload": {"live_bid": 5.0},
    }

    def active_producer() -> _QuoteSnapshotProducer:
        return next(
            producer
            for producer in _QUOTE_SNAPSHOT_PRODUCERS.values()
            if producer.key == ("5m", ((route.instrument_id, route.fingerprint),))
        )

    async def yielding_send_json(payload) -> None:
        nonlocal row_advanced, target_advanced
        await original_send_json(payload)
        await asyncio.sleep(0)
        producer = active_producer()
        if payload.get("type") == "quote_snapshot" and not row_advanced:
            producer.rows = [quote_row(instrument, price=101.0)]
            producer.rows_revision += 1
            row_advanced = True
        elif payload.get("type") == "option_targets_snapshot" and not target_advanced:
            producer.option_targets = [updated_target]
            producer.targets_revision = (producer.targets_revision or 0) + 1
            target_advanced = True

    websocket.send_json = yielding_send_json

    def received_raced_revisions() -> bool:
        received_row = any(
            message.get("type") == "quote_delta"
            and message.get("rows", [{}])[0].get("price") == 101.0
            for message in websocket.messages
        )
        received_target = any(
            message.get("type") == "option_targets_snapshot"
            and message.get("option_targets") == [updated_target]
            for message in websocket.messages
        )
        return received_row and received_target

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    quote_stream_seconds=10.0,
                    server_sleeping=received_raced_revisions,
                ),
            ),
            timeout=1.0,
        )

    asyncio.run(scenario())

    assert row_advanced is True
    assert target_advanced is True
    assert received_raced_revisions() is True


def test_quote_stream_sends_empty_delta_for_cache_generation_only_change() -> None:
    instrument = provider_identity("SPY", "ibkr")
    websocket = FakeWebSocket()
    calls = 0

    def rows(_instruments, *_args):
        nonlocal calls
        calls += 1
        return [
            quote_row(
                instrument,
                quote_received_at="2026-07-16T14:45:01+00:00",
                quote_age_seconds=float(calls),
                trend_interval="5m",
                trend_points=[{"ts": f"point-{calls}", "price": float(calls)}],
            )
        ]

    def cache_read(_instruments, *, after_revision=None):
        generation = calls
        return (
            None if after_revision == quote_cache_revision(generation) else {},
            "",
            quote_cache_revision(generation),
        )

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    screener_rows=rows,
                    quote_cache_for_instruments=cache_read,
                    server_sleeping=lambda: (
                        calls >= 4
                        and any(
                            message.get("type") == "quote_delta" for message in websocket.messages
                        )
                    ),
                ),
            ),
            timeout=1.0,
        )

    asyncio.run(scenario())

    assert calls >= 4
    snapshots = [
        message for message in websocket.messages if message.get("type") == "quote_snapshot"
    ]
    deltas = [message for message in websocket.messages if message.get("type") == "quote_delta"]
    assert len(snapshots) == 1
    assert deltas
    assert all(message["rows"] == [] for message in deltas)
    assert deltas[-1]["revision"] == snapshots[0]["revision"]
    assert deltas[-1]["cache_epoch"] == snapshots[0]["cache_epoch"]
    assert deltas[-1]["cache_generation"] > snapshots[0]["cache_generation"]


def test_quote_stream_reports_row_failure_recovery_and_same_failure_again() -> None:
    instrument = provider_identity("SPY", "ibkr")
    websocket = FakeWebSocket()
    calls = 0

    def rows(_instruments, *_args):
        nonlocal calls
        calls += 1
        if calls in {2, 4}:
            raise RuntimeError("row projection unavailable")
        return [quote_row(instrument)]

    def cache_read(_instruments, *, after_revision=None):
        generation = calls
        return (
            None if after_revision == quote_cache_revision(generation) else {},
            "",
            quote_cache_revision(generation),
        )

    def sleeping() -> bool:
        failures = [
            message
            for message in websocket.messages
            if message.get("type") == "quote_aux_status"
            and message.get("warnings", {}).get("quote_rows") == "row projection unavailable"
        ]
        return len(failures) >= 2

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    screener_rows=rows,
                    quote_cache_for_instruments=cache_read,
                    server_sleeping=sleeping,
                    quote_stream_seconds=0.12,
                ),
            ),
            timeout=2.0,
        )

    asyncio.run(scenario())

    health = [
        message for message in websocket.messages if message.get("type") == "quote_aux_status"
    ]
    row_health = [message.get("warnings", {}).get("quote_rows") for message in health]
    assert row_health[:3] == [
        "row projection unavailable",
        "",
        "row projection unavailable",
    ]
    assert [message["sequence"] for message in health] == sorted(
        message["sequence"] for message in health
    )


def test_quote_stream_compact_heartbeat_keeps_per_socket_sequence_contiguous() -> None:
    instrument = provider_identity("SPY", "ibkr")
    websocket = FakeWebSocket()

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    websocket_heartbeat_seconds=0.01,
                    server_sleeping=lambda: any(
                        message.get("type") == "quote_heartbeat" for message in websocket.messages
                    ),
                ),
            ),
            timeout=1.0,
        )

    asyncio.run(scenario())

    heartbeat = next(
        message for message in websocket.messages if message.get("type") == "quote_heartbeat"
    )
    assert "rows" not in heartbeat
    sequenced = [
        message["sequence"]
        for message in websocket.messages
        if "sequence" in message and message.get("type") != "live_candle_delta"
    ]
    assert sequenced == list(range(1, len(sequenced) + 1))


def test_live_candle_traffic_cannot_suppress_quote_state_heartbeat(monkeypatch) -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    trade_sequence = 0

    def read_events(_after_sequence, _routes):
        nonlocal trade_sequence
        trade_sequence += 1
        second = min(trade_sequence, 50)
        return (
            trade_sequence,
            [
                {
                    "sequence": trade_sequence,
                    "instrument_id": route.instrument_id,
                    "route_fingerprint": route.fingerprint,
                    "price": 100.0 + trade_sequence / 100.0,
                    "ts": f"2026-07-21T12:00:{second:02d}+00:00",
                    "gateway_ts": datetime.now(tz=UTC).isoformat(),
                }
            ],
            False,
        )

    monkeypatch.setattr(route.adapter, "read_quote_trade_events", read_events)

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    websocket_heartbeat_seconds=0.01,
                    server_sleeping=lambda: any(
                        message.get("type") == "quote_heartbeat" for message in websocket.messages
                    ),
                ),
            ),
            timeout=1.5,
        )

    asyncio.run(scenario())

    assert any(message.get("type") == "live_candle_delta" for message in websocket.messages)
    assert any(message.get("type") == "quote_heartbeat" for message in websocket.messages)


def test_quote_stream_rejects_missing_or_duplicate_rows_in_fixed_subscription() -> None:
    first = provider_identity("SPY", "ibkr")
    second = provider_identity("QQQ", "ibkr")
    websocket = FakeWebSocket()

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(first, second),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(first, second),
                screener_rows=lambda *_args: [quote_row(first), quote_row(first)],
            ),
        )
    )

    assert not any(message.get("type") == "quote_snapshot" for message in websocket.messages)
    assert any(
        message.get("type") == "quote_status" and message.get("error") == "QUOTE_STREAM_ERROR"
        for message in websocket.messages
    )


def test_quote_stream_aggregates_direct_ibkr_trade_events_without_persisting() -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("5m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="5m",
    )
    identity = (route.instrument_id, route.fingerprint)
    base = {
        "sequence": 1,
        "instrument_id": route.instrument_id,
        "symbol": "SPY",
        "route_fingerprint": route.fingerprint,
        "price": 754.20,
        "ts": "2026-07-16T14:47:01+00:00",
        "gateway_ts": "2026-07-16T14:47:01.050000+00:00",
    }

    assert asyncio.run(_update_quote_live_bars(producer, [base])) is True
    assert producer.live_bar_revisions[identity] == 1
    assert producer.live_bars[identity] == {
        "symbol": "SPY",
        "ts": "2026-07-16T14:45:00+00:00",
        "open": 754.20,
        "high": 754.20,
        "low": 754.20,
        "close": 754.20,
        "volume": 0.0,
        "timeframe": "5m",
        "source": "ibkr:trade-preview",
        "closed": False,
        "state": "forming",
        "provider": "ibkr",
        "instrument_id": route.instrument_id,
        "provider_symbol": "SPY",
        "route_fingerprint": route.fingerprint,
        "preview_kind": "broker_trade",
        "preview_ts": "2026-07-16T14:47:01+00:00",
        "gateway_ts": "2026-07-16T14:47:01.050000+00:00",
        "event_sequence": 1,
        "authoritative": False,
        "commit_pending": False,
        "expected_close": "2026-07-16T14:50:00+00:00",
    }

    assert (
        asyncio.run(
            _update_quote_live_bars(
                producer,
                [
                    {
                        **base,
                        "sequence": 2,
                        "price": 754.35,
                        "ts": "2026-07-16T14:47:02+00:00",
                    }
                ],
            )
        )
        is True
    )
    assert producer.live_bars[identity]["open"] == 754.20
    assert producer.live_bars[identity]["high"] == 754.35
    assert producer.live_bars[identity]["low"] == 754.20
    assert producer.live_bars[identity]["close"] == 754.35
    assert producer.live_bars[identity]["volume"] == 0.0
    assert producer.live_bars[identity]["preview_ts"] == "2026-07-16T14:47:02+00:00"
    assert producer.live_bars[identity]["event_sequence"] == 2

    assert (
        asyncio.run(
            _update_quote_live_bars(
                producer,
                [
                    {
                        **base,
                        "sequence": 3,
                        "price": 754.10,
                        "ts": "2026-07-16T14:50:00+00:00",
                    }
                ],
            )
        )
        is True
    )
    assert producer.live_bars[identity]["ts"] == "2026-07-16T14:50:00+00:00"
    assert producer.live_bars[identity]["open"] == 754.10
    assert producer.live_bars[identity]["volume"] == 0.0
    current_bar = dict(producer.live_bars[identity])
    assert (
        asyncio.run(
            _update_quote_live_bars(
                producer,
                [
                    {
                        **base,
                        "sequence": 4,
                        "price": 999.0,
                        "ts": "2026-07-16T14:49:59+00:00",
                    }
                ],
            )
        )
        is False
    )
    assert producer.live_bars[identity] == current_bar


def test_quote_trade_preview_does_not_wait_for_storage_or_server_slot() -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)

    class Store:
        def __getattr__(self, name):
            raise AssertionError(f"live preview must not read storage: {name}")

    producer = _QuoteSnapshotProducer(
        key=("5m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(store_factory=Store),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="5m",
    )
    changed = asyncio.run(
        _update_quote_live_bars(
            producer,
            [
                {
                    "sequence": 1,
                    "instrument_id": route.instrument_id,
                    "route_fingerprint": route.fingerprint,
                    "price": 754.20,
                    "ts": "2026-07-16T14:47:01+00:00",
                    "gateway_ts": "2026-07-16T14:47:01.050000+00:00",
                }
            ],
        )
    )

    assert changed is True
    preview = producer.live_bars[(route.instrument_id, route.fingerprint)]
    assert preview["ts"] == "2026-07-16T14:45:00+00:00"
    assert "bar_slot" not in preview


def test_quote_trade_preview_hot_path_has_no_storage_or_schedule_wait() -> None:
    source = inspect.getsource(quote_stream_ws._quote_trade_bar_axis)

    assert "await " not in source
    assert "to_thread" not in source
    assert "store_factory" not in source
    assert "provider_schedule" not in source


def test_quote_trade_preview_accepts_finite_signed_prices_and_rejects_guessed_events() -> None:
    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("5m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="5m",
    )
    identity = (route.instrument_id, route.fingerprint)
    base = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "ts": "2026-07-16T14:47:01+00:00",
        "gateway_ts": "2026-07-16T14:47:01.050000+00:00",
    }

    assert (
        asyncio.run(_update_quote_live_bars(producer, [{**base, "sequence": 0, "price": 0.0}]))
        is True
    )
    assert (
        asyncio.run(
            _update_quote_live_bars(
                producer,
                [{**base, "sequence": 1, "price": -37.63, "ts": "2026-07-16T14:47:02+00:00"}],
            )
        )
        is True
    )
    preview = producer.live_bars[identity]
    assert preview["open"] == 0.0
    assert preview["high"] == 0.0
    assert preview["low"] == -37.63
    assert preview["close"] == -37.63

    rejected = [
        {**base, "sequence": "2", "price": -38.0},
        {**base, "sequence": 2.0, "price": -38.0},
        {**base, "sequence": True, "price": -38.0},
        {**base, "sequence": -1, "price": -38.0},
        {**base, "sequence": 2, "price": float("nan")},
        {**base, "sequence": 2, "price": float("inf")},
        {**base, "sequence": 2, "price": -38.0, "ts": "2026-07-16T14:47:03"},
        {**base, "sequence": 2, "price": -38.0, "ts": datetime(2026, 7, 16, 14, 47, 3)},
    ]
    for event in rejected:
        assert asyncio.run(_update_quote_live_bars(producer, [event])) is False
    assert producer.live_bars[identity] == preview


def test_quote_stream_preview_requires_active_exact_route() -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("5m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="5m",
    )

    assert (
        asyncio.run(
            _update_quote_live_bars(
                producer,
                [
                    {
                        "sequence": 1,
                        "instrument_id": "ibkr|contract|wrong",
                        "symbol": "SPY",
                        "route_fingerprint": route.fingerprint,
                        "price": 754.20,
                        "ts": "2026-07-16T14:47:01+00:00",
                    },
                    {
                        "sequence": 2,
                        "instrument_id": route.instrument_id,
                        "symbol": "SPY",
                        "route_fingerprint": "wrong-route",
                        "price": 754.20,
                        "ts": "2026-07-16T14:47:01+00:00",
                    },
                ],
            )
        )
        is False
    )
    assert producer.live_bars == {}


def test_quote_stream_hour_preview_uses_epoch_grid_without_schedule_io() -> None:
    instrument = provider_identity("SPX", "ibkr")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("60m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="60m",
    )
    base = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "price": 754.20,
        "ts": "2026-07-16T14:47:01+00:00",
        "gateway_ts": "2026-07-16T14:47:01.050000+00:00",
    }

    assert asyncio.run(_update_quote_live_bars(producer, [{**base, "sequence": 1}])) is True
    assert (
        asyncio.run(
            _update_quote_live_bars(
                producer,
                [{**base, "sequence": 2, "ts": "2026-07-16T14:48:01+00:00"}],
            )
        )
        is True
    )

    identity = (route.instrument_id, route.fingerprint)
    assert producer.live_bars[identity]["ts"] == "2026-07-16T14:00:00+00:00"
    assert producer.live_bars[identity]["expected_close"] == "2026-07-16T15:00:00+00:00"


def test_quote_stream_opening_hour_preview_stays_provisional_until_provider_bar() -> None:
    instrument = provider_identity("SPX", "ibkr")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("60m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="60m",
    )
    event = {
        "sequence": 1,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "price": 754.20,
        "ts": "2026-07-16T13:47:01+00:00",
        "gateway_ts": "2026-07-16T13:47:01.050000+00:00",
    }

    assert asyncio.run(_update_quote_live_bars(producer, [event])) is True
    preview = producer.live_bars[(route.instrument_id, route.fingerprint)]
    assert preview["ts"] == "2026-07-16T13:00:00+00:00"
    assert preview["expected_close"] == "2026-07-16T14:00:00+00:00"
    assert preview["authoritative"] is False


def test_quote_stream_non_rth_hour_preview_needs_no_session_read() -> None:
    instrument = provider_identity("SPY", "ibkr", asset_class="stock")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("60m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="60m",
    )
    event = {
        "sequence": 1,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "price": 754.20,
        "ts": "2026-07-16T09:47:01+00:00",
        "gateway_ts": "2026-07-16T09:47:01.050000+00:00",
    }

    assert asyncio.run(_update_quote_live_bars(producer, [event])) is True
    identity = (route.instrument_id, route.fingerprint)
    assert producer.live_bars[identity]["ts"] == "2026-07-16T09:00:00+00:00"


def test_quote_stream_schedule_unknown_still_publishes_typed_preview() -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    producer = _QuoteSnapshotProducer(
        key=("60m", ((route.instrument_id, route.fingerprint),)),
        deps=_deps(),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[],
        interval="60m",
    )
    event = {
        "sequence": 1,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "price": 754.20,
        "ts": "2026-07-16T14:47:01+00:00",
        "gateway_ts": "2026-07-16T14:47:01.050000+00:00",
    }

    assert asyncio.run(_update_quote_live_bars(producer, [event])) is True
    assert asyncio.run(_update_quote_live_bars(producer, [{**event, "sequence": 2}])) is True
    preview = producer.live_bars[(route.instrument_id, route.fingerprint)]
    assert preview["closed"] is False
    assert "bar_slot" not in preview


def test_quote_producer_initial_failure_always_releases_ready_waiter() -> None:
    async def run() -> None:
        instrument = provider_identity("SPY", "ibkr")
        route = route_instrument(instrument)

        def fail_selected():
            raise RuntimeError("catalog unavailable")

        producer = _QuoteSnapshotProducer(
            key=("5m", ((route.instrument_id, route.fingerprint),)),
            deps=_deps(quote_route_snapshot=fail_selected),
            instruments=[instrument],
            requested_routes=parse_route_selection(route_query(instrument)),
            quote_wanted_keys=[],
            interval="5m",
        )
        await asyncio.wait_for(_run_quote_snapshot_producer(producer), timeout=0.5)

        assert producer.ready.is_set()
        assert producer.error == "catalog unavailable"

    asyncio.run(run())


def test_quote_route_selection_reads_process_snapshot_without_threadpool_hop() -> None:
    source = inspect.getsource(quote_stream_ws)
    compact_source = "".join(source.split())

    assert "route_task" not in source
    assert "asyncio.to_thread(resolve_route_selection" not in compact_source
    assert source.count("resolve_route_selection(") == 2
    assert source.count("await_cancellation_deferred_task(") == 2
    assert "await asyncio.shield(release_task)" not in source


def test_quote_producer_reuses_qualification_until_canonical_route_snapshot_changes(
    monkeypatch,
) -> None:
    import importlib

    quote_runtime = importlib.import_module("aef_terminal.ui.runtime.quote_stream")

    instrument = provider_identity("SPY", "ibkr")
    initial = quote_route_snapshot([instrument])
    metadata_changed = quote_route_snapshot([{**instrument, "display": "Updated display"}])
    retired = quote_route_snapshot([])
    current = [initial]
    producer = _QuoteSnapshotProducer(
        key=("snapshot-reuse",),
        deps=_deps(quote_route_snapshot=lambda: current[0]),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[instrument["instrument_id"]],
        interval="5m",
    )
    work = []
    original_route = quote_stream_ws.route_instrument
    original_wire = quote_runtime.domain_wire_value

    def qualify(*args, **kwargs):
        work.append("qualify")
        return original_route(*args, **kwargs)

    def wire(*args, **kwargs):
        work.append("wire")
        return original_wire(*args, **kwargs)

    monkeypatch.setattr(quote_stream_ws, "route_instrument", qualify)
    monkeypatch.setattr(quote_runtime, "domain_wire_value", wire)
    assert producer.refresh_instruments()
    assert work
    work.clear()
    monkeypatch.setattr(
        producer.routes[0].adapter,
        "read_quote_trade_events",
        lambda sequence, _identities: (max(sequence, 0), [], False),
    )

    async def run():
        for _ in range(50):
            assert not producer.refresh_instruments()
            identities = quote_stream_ws._producer_route_pairs(producer)
            quote_stream_ws._read_quote_trade_events(producer, identities)
            quote_stream_ws._screener_base_generations(producer.routes, "5m")
            await _update_quote_live_bars(producer, [])

    asyncio.run(run())
    assert work == []
    current[0] = metadata_changed
    assert producer.refresh_instruments()
    assert producer.instruments[0]["display"] == "Updated display"
    assert work.count("wire") == 1
    work.clear()
    assert not producer.refresh_instruments()
    assert work == []
    current[0] = retired
    with pytest.raises(quote_stream_ws.RouteSelectionMismatch):
        producer.refresh_instruments()
    assert producer.route_snapshot is metadata_changed


def test_quote_producer_cleanup_grace_reuses_inflight_producer_on_reconnect() -> None:
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    key = ("5m", ((route.instrument_id, route.fingerprint),))
    wanted: list[list[str]] = []
    producer = _QuoteSnapshotProducer(
        key=key,
        deps=_deps(
            quote_stream_cleanup_grace_seconds=0.02,
            set_quote_stream_wanted=lambda _key, values: wanted.append(list(values)),
        ),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[route.instrument_id],
        interval="5m",
        consumers=1,
    )

    async def scenario() -> None:
        _QUOTE_SNAPSHOT_PRODUCERS[key] = producer
        release = asyncio.create_task(_release_quote_snapshot_producer(producer))
        await asyncio.sleep(0)
        assert producer.consumers == 0
        assert _QUOTE_SNAPSHOT_PRODUCERS[key] is producer
        producer.consumers += 1
        await release
        assert _QUOTE_SNAPSHOT_PRODUCERS[key] is producer
        assert wanted == []
        await _release_quote_snapshot_producer(producer)
        assert key not in _QUOTE_SNAPSHOT_PRODUCERS
        assert wanted == [[]]

    try:
        asyncio.run(scenario())
    finally:
        _QUOTE_SNAPSHOT_PRODUCERS.pop(key, None)


def test_quote_producer_cancellation_settles_physical_snapshot_read() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)

    def blocking_bases(_instruments, _interval):
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return {}

    async def scenario() -> None:
        producer = _QuoteSnapshotProducer(
            key=("physical-settlement",),
            deps=_deps(
                quote_route_snapshot=quote_routes(instrument),
                screener_bases=blocking_bases,
            ),
            instruments=[instrument],
            requested_routes=parse_route_selection(route_query(instrument)),
            quote_wanted_keys=[route.instrument_id],
            interval="5m",
        )
        task = asyncio.create_task(_run_quote_snapshot_producer(producer))
        assert await asyncio.to_thread(started.wait, 1)

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("producer cancellation must propagate after physical settlement")
        assert finished.is_set() is True

    asyncio.run(scenario())


def test_run_quote_stream_pushes_direct_trade_preview_bar(monkeypatch) -> None:
    websocket = FakeWebSocket()
    sleeping = iter([False, False, True, True])
    sequence = 0
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    gateway_ts = datetime.now(tz=UTC).isoformat()

    def read_events(after_sequence, _symbols):
        nonlocal sequence
        if sequence == 0:
            sequence = 1
            return (
                1,
                [
                    {
                        "sequence": 1,
                        "instrument_id": route.instrument_id,
                        "symbol": "SPY",
                        "route_fingerprint": route.fingerprint,
                        "price": 754.20,
                        "ts": "2026-07-16T14:47:01+00:00",
                        "gateway_ts": gateway_ts,
                        "source": "ibkr:last",
                    }
                ],
                False,
            )
        return max(sequence, after_sequence), [], False

    monkeypatch.setattr(route.adapter, "read_quote_trade_events", read_events)

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                server_sleeping=lambda: next(sleeping),
                screener_rows=lambda *_args: [quote_row(instrument)],
            ),
        )
    )

    live_delta = next(
        message for message in websocket.messages if message.get("type") == "live_candle_delta"
    )
    assert live_delta["sequence"] == 1
    assert live_delta["loss_after_sequence"] is None
    assert live_delta["bars"] == [
        {
            "symbol": "SPY",
            "ts": "2026-07-16T14:45:00+00:00",
            "open": 754.20,
            "high": 754.20,
            "low": 754.20,
            "close": 754.20,
            "volume": 0.0,
            "timeframe": "5m",
            "source": "ibkr:trade-preview",
            "closed": False,
            "state": "forming",
            "provider": "ibkr",
            "instrument_id": route.instrument_id,
            "provider_symbol": "SPY",
            "route_fingerprint": route.fingerprint,
            "preview_kind": "broker_trade",
            "preview_ts": "2026-07-16T14:47:01+00:00",
            "gateway_ts": gateway_ts,
            "event_sequence": 1,
            "authoritative": False,
            "commit_pending": False,
            "expected_close": "2026-07-16T14:50:00+00:00",
        }
    ]
    assert all(
        "live_bars" not in message
        for message in websocket.messages
        if message.get("type") == "quote_snapshot"
    )


def test_quote_trade_cursor_advances_without_axis_admission_or_storage(
    monkeypatch,
) -> None:
    websocket = FakeWebSocket()
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    observed_cursors: list[int] = []
    gateway_ts = datetime.now(tz=UTC).isoformat()

    class Store:
        def read_option_targets(self, **_kwargs):
            return []

        def read_price_alerts(self, **_kwargs):
            return []

    first_event = {
        "sequence": 1,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "price": 754.20,
        "ts": "2026-07-16T14:47:01+00:00",
        "gateway_ts": gateway_ts,
    }
    second_event = {
        **first_event,
        "sequence": 2,
        "price": 754.35,
        "ts": "2026-07-16T14:47:02+00:00",
    }

    def read_events(after_sequence, _routes):
        observed_cursors.append(after_sequence)
        return (2, [first_event, second_event], False) if after_sequence < 2 else (2, [], False)

    monkeypatch.setattr(route.adapter, "read_quote_trade_events", read_events)

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    store_factory=Store,
                    quote_route_snapshot=quote_routes(instrument),
                    server_sleeping=lambda: (
                        any(
                            message.get("type") == "live_candle_delta"
                            for message in websocket.messages
                        )
                        and 2 in observed_cursors[1:]
                    ),
                    screener_rows=lambda *_args: [quote_row(instrument)],
                ),
            ),
            timeout=1.0,
        )

    asyncio.run(scenario())

    live_delta = next(
        message for message in websocket.messages if message.get("type") == "live_candle_delta"
    )
    assert live_delta["bars"][0]["event_sequence"] == 2
    assert live_delta["bars"][0]["high"] == 754.35
    assert observed_cursors[0] == -1
    assert 2 in observed_cursors[1:]


def test_run_quote_stream_marks_compact_delta_after_trade_event_overflow(monkeypatch) -> None:
    websocket = FakeWebSocket()
    sleeping = iter([False, False, True, True])
    instrument = provider_identity("SPY", "ibkr")
    route = route_instrument(instrument)
    delivered = False
    gateway_ts = datetime.now(tz=UTC).isoformat()

    def read_events(after_sequence, _routes):
        nonlocal delivered
        if not delivered:
            delivered = True
            return (
                9,
                [
                    {
                        "sequence": 9,
                        "instrument_id": route.instrument_id,
                        "symbol": "SPY",
                        "route_fingerprint": route.fingerprint,
                        "price": 754.20,
                        "ts": "2026-07-16T14:47:01+00:00",
                        "gateway_ts": gateway_ts,
                        "source": "ibkr:last",
                    }
                ],
                True,
            )
        return max(after_sequence, 9), [], False

    monkeypatch.setattr(route.adapter, "read_quote_trade_events", read_events)

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                server_sleeping=lambda: next(sleeping),
                screener_rows=lambda *_args: [quote_row(instrument)],
            ),
        )
    )

    live_delta = next(
        message for message in websocket.messages if message.get("type") == "live_candle_delta"
    )
    assert live_delta["sequence"] == 1
    assert live_delta["loss_after_sequence"] == 1
    assert len(live_delta["bars"]) == 1


def test_run_quote_stream_sends_sleep_status_without_starting_stream() -> None:
    websocket = FakeWebSocket()
    started = []
    instrument = provider_identity("RISK", "ibkr")

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(server_sleeping=lambda: True, quote_stream_started=lambda: started.append(True)),
        )
    )

    assert websocket.accepted is True
    assert started == []
    assert websocket.messages[0]["source"] == "server:sleep"
    assert websocket.messages[0]["type"] == "quote_status"
    assert websocket.messages[0]["code"] == "SERVER_SLEEPING"


def test_run_quote_stream_rejects_legacy_parallel_route_query() -> None:
    websocket = FakeWebSocket()
    started = []

    asyncio.run(
        run_quote_stream(
            websocket,
            "instrument-a,instrument-b",
            "5m",
            _deps(quote_stream_started=lambda: started.append(True)),
        )
    )

    assert websocket.accepted is True
    assert started == []
    assert websocket.messages == [{"type": "quote_status", "error": "QUOTE_STREAM_ROUTES_INVALID"}]


def test_run_quote_stream_registers_wanted_instrument_ids_and_cleans_up_on_sleep() -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    sleeping = iter([False, True, True])
    wanted = []
    finished = []

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                server_sleeping=lambda: next(sleeping),
                set_quote_stream_wanted=lambda _key, symbols: wanted.append(list(symbols)),
                quote_stream_finished=lambda: finished.append(True) or 0,
            ),
        )
    )

    assert wanted[0] == [route.instrument_id]
    assert wanted[-1] == []
    assert finished == [True]
    assert websocket.messages[0]["source"] == "server:sleep"
    assert websocket.messages[0]["type"] == "quote_status"


def test_run_quote_stream_pushes_persisted_option_target_state() -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    target = {
        "id": "opt-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "RISK",
        "timeframe": "5m",
        "payload": {"live_bid": 4.1, "live_ask": 4.3},
    }

    class Store:
        def read_option_targets(self, *, route_pairs, timeframe):
            assert (route_pairs, timeframe) == (
                ((route.instrument_id, route.fingerprint),),
                "5m",
            )
            return [{"payload": target}]

        def read_price_alerts(self, **_kwargs):
            return []

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                server_sleeping=lambda: any(
                    message.get("type") == "option_targets_snapshot"
                    and message.get("option_targets") == [target]
                    for message in websocket.messages
                ),
                store_factory=Store,
            ),
        )
    )

    target_snapshot = next(
        message
        for message in websocket.messages
        if message.get("type") == "option_targets_snapshot"
        and message.get("option_targets") == [target]
    )
    assert target_snapshot["option_targets"] == [target]


def test_quote_stream_pushes_only_meaningful_price_alert_runtime_state(
    monkeypatch,
) -> None:
    source = inspect.getsource(quote_stream_ws._QuoteStreamConsumer.send_alert_runtime)
    assert "self.producer.alert_runtime_error" in source

    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    stored_alert = {
        "id": "alert-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "symbol": "RISK",
        "provider": "ibkr",
        "provider_contract_id": "definition-only",
        "kind": "price",
        "label": "",
        "direction": "above",
        "price": 101.0,
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "rearmMinutes": 60,
        "createdAt": 1_000,
        "level_source": {"type": "fixed_price", "dynamic": False},
        "enabled": True,
        "armed": False,
        "fired": True,
        "cooldownUntil": 1234,
        "rearmedAt": 1100,
        "lastFiredAt": 1200,
        "lastFiredLevel": 101.0,
        "lastTelegramStatus": "send_failed",
        "lastTelegramOk": False,
        "telegramDeliveryStatus": "pending",
        "telegramNextRetryAt": 1300,
        "telegramRetryCount": 2,
        "telegramPendingSince": 1210,
        "telegramClaimedAt": 0,
        "lastTelegramDetail": "delivery detail",
        "telegramLastAttemptAt": 1220,
        "telegramLastError": "transient delivery detail",
        "touchDirection": "from_below",
        "wasTouching": True,
        "lastEventTs": "2026-07-21T22:00:00+00:00",
        "lastFiredEventTs": "2026-07-21T21:59:59+00:00",
        "lastSeenPrice": 100.75,
        "lastSeenLevel": 100.5,
        "lastTolerance": 0.25,
    }
    stored_alert["telegramPendingPayload"] = alert_monitor_payload(
        stored_alert,
        100.75,
        trigger_event_at=1_210,
    )

    class Store:
        def read_option_targets(self, **_kwargs):
            return []

        def read_price_alerts(self, **_kwargs):
            raise AssertionError("quote stream must not read the alert table")

    registry = AlertRuntimeRegistry()
    registry.hydrate([stored_alert])
    monkeypatch.setattr(quote_stream_ws, "price_alert_runtime", registry)
    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                store_factory=Store,
                server_sleeping=lambda: any(
                    message.get("type") == "quote_heartbeat" and "price_alert_runtime" in message
                    for message in websocket.messages
                ),
            ),
        )
    )

    runtime_snapshot = next(
        message
        for message in websocket.messages
        if message.get("type") == "quote_heartbeat" and "price_alert_runtime" in message
    )
    legacy_state_types = {
        "quote_snapshot",
        "quote_delta",
        "quote_heartbeat",
        "option_targets_snapshot",
        "fast_indicators_snapshot",
        "quote_aux_status",
    }
    sequenced_messages = [
        message for message in websocket.messages if isinstance(message.get("sequence"), int)
    ]
    assert all(message["type"] in legacy_state_types for message in sequenced_messages)
    assert [message["sequence"] for message in sequenced_messages] == list(
        range(1, len(sequenced_messages) + 1)
    )
    assert runtime_snapshot["alert_runtime_revision"] == 1
    assert runtime_snapshot["price_alert_runtime"] == [
        {
            "id": "alert-1",
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "timeframe": "5m",
            "armed": False,
            "fired": True,
            "cooldownUntil": 1234,
            "rearmedAt": 1100,
            "lastFiredAt": 1200,
            "lastFiredLevel": 101.0,
            "lastTelegramStatus": "send_failed",
            "lastTelegramOk": False,
            "telegramDeliveryStatus": "pending",
            "telegramNextRetryAt": 1300,
            "telegramRetryCount": 2,
            "touchDirection": "from_below",
            "lastEventTs": "2026-07-21T22:00:00+00:00",
            "lastFiredEventTs": "2026-07-21T21:59:59+00:00",
        }
    ]
    runtime = runtime_snapshot["price_alert_runtime"][0]
    for excluded in (
        "symbol",
        "provider",
        "provider_contract_id",
        "kind",
        "direction",
        "price",
        "lastSeenPrice",
        "lastSeenLevel",
        "lastTolerance",
        "lastTelegramDetail",
        "telegramPendingSince",
        "telegramClaimedAt",
        "telegramLastAttemptAt",
        "telegramLastError",
        "telegramPendingPayload",
        "wasTouching",
    ):
        assert excluded not in runtime


def test_price_alert_runtime_scope_excludes_another_timeframe_without_db_read(monkeypatch) -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    invalid_alert = {
        "id": "alert-wrong-timeframe",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "15m",
        "symbol": "RISK",
        "provider": "ibkr",
        "provider_contract_id": "definition-only",
        "kind": "price",
        "label": "",
        "direction": "cross",
        "price": 100.0,
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 1,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }

    class Store:
        def read_option_targets(self, **_kwargs):
            return []

        def read_price_alerts(self, **_kwargs):
            raise AssertionError("quote stream must not read the alert table")

    registry = AlertRuntimeRegistry()
    registry.hydrate([invalid_alert])
    monkeypatch.setattr(quote_stream_ws, "price_alert_runtime", registry)
    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                store_factory=Store,
                server_sleeping=lambda: any(
                    message.get("type") == "quote_heartbeat" and "price_alert_runtime" in message
                    for message in websocket.messages
                ),
            ),
        )
    )

    snapshot = next(
        message
        for message in websocket.messages
        if message.get("type") == "quote_heartbeat" and "price_alert_runtime" in message
    )
    assert snapshot["price_alert_runtime"] == []
    assert snapshot.get("warnings", {}).get("price_alert_runtime", "") == ""


def test_price_alert_runtime_publishes_committed_registry_revisions_without_db_poll(
    monkeypatch,
) -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    stored_alert = {
        "id": "alert-retained",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "symbol": "RISK",
        "provider": "ibkr",
        "provider_contract_id": "definition-only",
        "kind": "price",
        "label": "",
        "direction": "cross",
        "price": 100.0,
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        "enabled": True,
        "armed": False,
        "fired": True,
        "cooldownUntil": 2_000,
        "rearmedAt": 1,
        "lastFiredAt": 1234,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }

    class Store:
        def read_option_targets(self, **_kwargs):
            return []

        def read_price_alerts(self, **_kwargs):
            raise AssertionError("quote stream must not read the alert table")

    published = False

    def completed() -> bool:
        nonlocal published
        runtime_snapshots = [
            message
            for message in websocket.messages
            if message.get("type") == "quote_heartbeat" and "price_alert_runtime" in message
        ]
        if runtime_snapshots and not published:
            published = True
            registry.publish_committed(
                {
                    **stored_alert,
                    "lastFiredAt": 2345,
                    "lastSeenPrice": 999.0,
                }
            )
        return len(runtime_snapshots) >= 2

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    store_factory=Store,
                    server_sleeping=completed,
                ),
            ),
            timeout=3.5,
        )

    registry = AlertRuntimeRegistry()
    registry.hydrate([stored_alert])
    monkeypatch.setattr(quote_stream_ws, "price_alert_runtime", registry)
    asyncio.run(scenario())

    runtime_snapshots = [
        message
        for message in websocket.messages
        if message.get("type") == "quote_heartbeat" and "price_alert_runtime" in message
    ]
    assert len(runtime_snapshots) == 2
    assert runtime_snapshots[0]["price_alert_runtime"][0]["id"] == "alert-retained"
    assert runtime_snapshots[0]["price_alert_runtime"][0]["lastFiredAt"] == 1234
    assert runtime_snapshots[0]["alert_runtime_revision"] == 1
    assert runtime_snapshots[1]["price_alert_runtime"][0]["lastFiredAt"] == 2345
    assert runtime_snapshots[1]["alert_runtime_revision"] == 2
    assert not any(
        message.get("warnings", {}).get("price_alert_runtime") for message in websocket.messages
    )


def _target_read_runtime(store_factory):
    instrument = provider_identity("SPY", "ibkr")
    producer = _QuoteSnapshotProducer(
        key=("target-read",),
        deps=_deps(quote_route_snapshot=quote_routes(instrument), store_factory=store_factory),
        instruments=[instrument],
        requested_routes=parse_route_selection(route_query(instrument)),
        quote_wanted_keys=[instrument["instrument_id"]],
        interval="5m",
    )
    return quote_stream_ws._QuoteProducerRuntime(producer)


def test_option_target_stream_skips_unchanged_reads_and_publishes_deletion():
    reads = []
    factories = []
    rows = []
    main_thread = threading.get_ident()

    class Store:
        def read_option_targets(self, **_kwargs):
            assert threading.get_ident() != main_thread
            reads.append(1)
            return list(rows)

    def factory():
        assert threading.get_ident() != main_thread
        factories.append(1)
        return Store()

    runtime = _target_read_runtime(factory)
    route = runtime.producer.routes[0]
    target = {
        "id": "target",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
    }

    async def refresh(now):
        runtime.schedule_targets(now)
        if runtime.targets_task is not None:
            await runtime.targets_task
            runtime.complete_targets()

    async def run():
        await refresh(0.0)
        for i in range(1, 1001):
            await refresh(float(i))
        assert len(reads) == len(factories) == 1
        assert runtime.producer.option_targets == []
        assert runtime.producer.targets_revision == 1
        with option_target_mutation() as mutation:
            rows.append({"payload": target})
            mutation.changed = True
        await refresh(1001.0)
        assert runtime.producer.option_targets == [target]
        with option_target_mutation() as mutation:
            rows.clear()
            mutation.changed = True
        await refresh(1002.0)
        assert runtime.producer.option_targets == []
        assert runtime.producer.targets_revision == 3
        assert len(reads) == 3

    asyncio.run(run())


def test_option_target_stream_discards_read_invalidated_before_publication():
    reads = 0

    class Store:
        def read_option_targets(self, **_kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                with option_target_mutation() as mutation:
                    mutation.changed = True
                return [{"payload": {"id": "stale"}}]
            return []

    runtime = _target_read_runtime(Store)

    async def run():
        runtime.schedule_targets(0.0)
        await runtime.targets_task
        runtime.complete_targets()
        assert runtime.producer.targets_revision is None
        assert runtime.producer.targets_error == ""
        runtime.schedule_targets(0.1)
        await runtime.targets_task
        runtime.complete_targets()
        assert runtime.producer.option_targets == []
        assert runtime.producer.targets_revision == 1
        assert reads == 2

    asyncio.run(run())


def test_option_target_stream_scope_change_invalidates_same_revision():
    selected = []

    class Store:
        def read_option_targets(self, *, route_pairs, timeframe):
            selected.append(route_pairs)
            return [
                {
                    "payload": {
                        "id": "target",
                        "instrument_id": route_pairs[0][0],
                        "route_fingerprint": route_pairs[0][1],
                        "timeframe": timeframe,
                    }
                }
            ]

    runtime = _target_read_runtime(Store)

    async def run():
        runtime.schedule_targets(0.0)
        await runtime.targets_task
        runtime.producer._set_instruments([provider_identity("QQQ", "ibkr")])
        runtime.complete_targets()
        assert runtime.producer.targets_revision is None
        runtime.schedule_targets(0.1)
        await runtime.targets_task
        runtime.complete_targets()
        assert runtime.producer.option_targets[0]["instrument_id"] == selected[-1][0][0]
        assert selected[0] != selected[1]

    asyncio.run(run())


def test_option_target_stream_cancellation_settles_storage_initialization():
    started, release, completed = threading.Event(), threading.Event(), threading.Event()

    def factory():
        started.set()
        assert release.wait(2.0)
        completed.set()
        return type("Store", (), {"read_option_targets": lambda self, **_kwargs: []})()

    runtime = _target_read_runtime(factory)

    async def run():
        runtime.schedule_targets(0.0)
        assert await asyncio.to_thread(started.wait, 1.0)
        cancellation = asyncio.create_task(runtime.cancel())
        await asyncio.sleep(0)
        cancellation.cancel()
        await asyncio.sleep(0)
        assert not cancellation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancellation
        assert completed.is_set()

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_option_targets_retain_last_good_snapshot_during_read_failure() -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    target = {
        "id": "target-retained",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "point": {"price": 100.0},
    }
    target_reads = 0
    mutation_sent = False

    class Store:
        def read_option_targets(self, **_kwargs):
            nonlocal target_reads
            target_reads += 1
            if target_reads == 2:
                raise RuntimeError("option targets unavailable")
            next_target = (
                target
                if target_reads == 1
                else {
                    **target,
                    "point": {"price": 101.0},
                }
            )
            return [{"payload": next_target}]

        def read_price_alerts(self, **_kwargs):
            return []

    def recovered() -> bool:
        nonlocal mutation_sent
        target_health = [
            message.get("warnings", {}).get("option_targets")
            for message in websocket.messages
            if message.get("type") == "quote_aux_status"
        ]
        target_snapshots = [
            message
            for message in websocket.messages
            if message.get("type") == "option_targets_snapshot"
        ]
        if target_snapshots and not mutation_sent:
            with option_target_mutation() as mutation:
                mutation.changed = True
            mutation_sent = True
        return (
            "option targets unavailable" in target_health
            and target_health[-1:] == [""]
            and len(target_snapshots) >= 2
        )

    async def scenario() -> None:
        await asyncio.wait_for(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    store_factory=Store,
                    server_sleeping=recovered,
                ),
            ),
            timeout=3.5,
        )

    asyncio.run(scenario())

    target_snapshots = [
        message
        for message in websocket.messages
        if message.get("type") == "option_targets_snapshot"
    ]
    assert len(target_snapshots) == 2
    assert target_snapshots[0]["revision"] == 1
    assert target_snapshots[0]["option_targets"] == [target]
    assert target_snapshots[1]["revision"] == 2
    assert target_snapshots[1]["option_targets"][0]["point"]["price"] == 101.0
    assert not any(message.get("option_targets") == [] for message in websocket.messages)
    assert target_reads >= 3


def test_quote_stream_rejects_option_target_outside_exact_subscription() -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    invalid_target = {
        "id": "opt-wrong-route",
        "instrument_id": route.instrument_id,
        "route_fingerprint": "wrong-route",
        "timeframe": "5m",
    }

    class Store:
        def read_option_targets(self, **_kwargs):
            return [{"payload": invalid_target}]

        def read_price_alerts(self, **_kwargs):
            return []

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                store_factory=Store,
                server_sleeping=lambda: any(
                    message.get("warnings", {}).get("option_targets")
                    == "option target must match the exact quote subscription"
                    for message in websocket.messages
                ),
            ),
        )
    )

    assert not any(
        invalid_target in message.get("option_targets", []) for message in websocket.messages
    )
    assert not any(
        message.get("type") == "option_targets_snapshot" for message in websocket.messages
    )
    assert any(
        message.get("warnings", {}).get("option_targets")
        == "option target must match the exact quote subscription"
        for message in websocket.messages
    )


def test_quote_stream_waits_for_initial_bases_while_option_reads_are_blocked() -> None:
    instrument = provider_identity("RISK", "ibkr")
    route = route_instrument(instrument)
    websocket = FakeWebSocket()
    bases_started = threading.Event()
    bases_release = threading.Event()
    targets_started = threading.Event()
    targets_release = threading.Event()
    sleeping = False

    def screener_bases(_instruments, _interval):
        bases_started.set()
        bases_release.wait(timeout=2.0)
        return {route.instrument_id: {"previous_session_close": 99.0}}

    def screener_rows(instruments, bases, _live_map, warning, _interval):
        assert bases == {route.instrument_id: {"previous_session_close": 99.0}}
        return [
            quote_row(
                instrument,
                warning=warning,
                change=1.0,
                change_pct=1.010101,
                change_base=99.0,
            )
            for instrument in instruments
        ]

    class Store:
        def read_option_targets(self, **_kwargs):
            targets_started.set()
            targets_release.wait(timeout=2.0)
            return []

        def read_price_alerts(self, **_kwargs):
            raise AssertionError("quote stream must not read the alert table")

    async def scenario() -> None:
        nonlocal sleeping
        task = asyncio.create_task(
            run_quote_stream(
                websocket,
                route_query(instrument),
                "5m",
                _deps(
                    quote_route_snapshot=quote_routes(instrument),
                    server_sleeping=lambda: sleeping,
                    screener_bases=screener_bases,
                    screener_rows=screener_rows,
                    store_factory=Store,
                ),
            )
        )
        for _ in range(50):
            if bases_started.is_set() and targets_started.is_set():
                break
            await asyncio.sleep(0.01)

        assert bases_started.is_set()
        assert targets_started.is_set()
        assert not any(message.get("type") == "quote_snapshot" for message in websocket.messages)
        bases_release.set()
        for _ in range(50):
            if any(message.get("type") == "quote_snapshot" for message in websocket.messages):
                break
            await asyncio.sleep(0.01)
        snapshot = next(
            message for message in websocket.messages if message.get("type") == "quote_snapshot"
        )
        assert snapshot["rows"][0]["change_base"] == 99.0
        assert snapshot["rows"][0]["change_pct"] == 1.010101
        sleeping = True
        await asyncio.sleep(0.05)
        assert task.done() is False
        targets_release.set()
        await asyncio.wait_for(task, timeout=0.5)

    try:
        asyncio.run(scenario())
    finally:
        bases_release.set()
        targets_release.set()


def test_quote_screener_bases_refresh_only_changed_canonical_routes(monkeypatch) -> None:
    first = provider_identity("FIRST", "ibkr")
    second = provider_identity("SECOND", "ibkr")
    first_route = route_instrument(first)
    second_route = route_instrument(second)
    generations = {
        (first_route.instrument_id, first_route.fingerprint): 0,
        (second_route.instrument_id, second_route.fingerprint): 0,
    }
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        quote_stream_ws,
        "_SCREENER_BASE_GENERATION_POLL_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        quote_stream_ws,
        "_SCREENER_BASE_SAFETY_REFRESH_SECONDS",
        60.0,
    )
    monkeypatch.setattr(
        quote_stream_ws,
        "_screener_base_generations",
        lambda _instruments, _interval: dict(generations),
    )

    def screener_bases(instruments, _interval):
        identities = tuple(route_instrument(instrument).instrument_id for instrument in instruments)
        calls.append(identities)
        return {instrument_id: {"previous_session_close": 99.0} for instrument_id in identities}

    async def scenario() -> None:
        deps = _deps(
            quote_route_snapshot=quote_routes(first, second),
            screener_bases=screener_bases,
        )
        producer = _QuoteSnapshotProducer(
            key=("generation-refresh",),
            deps=deps,
            instruments=[first, second],
            requested_routes=parse_route_selection(route_query(first, second)),
            quote_wanted_keys=[first_route.instrument_id, second_route.instrument_id],
            interval="5m",
        )
        task = asyncio.create_task(_run_quote_snapshot_producer(producer))
        try:
            for _ in range(100):
                if len(calls) == 1 and producer.rows is not None:
                    break
                await asyncio.sleep(0.005)
            assert calls == [(first_route.instrument_id, second_route.instrument_id)]

            generations[(second_route.instrument_id, second_route.fingerprint)] = 1
            for _ in range(100):
                if len(calls) == 2:
                    break
                await asyncio.sleep(0.005)
            assert calls == [
                (first_route.instrument_id, second_route.instrument_id),
                (second_route.instrument_id,),
            ]

            await asyncio.sleep(0.02)
            assert len(calls) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_quote_stream_marks_auxiliary_snapshot_failures_as_degraded() -> None:
    instrument = provider_identity("RISK", "ibkr")
    websocket = FakeWebSocket()

    def fail_bases(_instruments, _interval):
        raise RuntimeError("bases unavailable")

    class Store:
        def read_option_targets(self, **_kwargs):
            raise RuntimeError("targets unavailable")

        def read_price_alerts(self, **_kwargs):
            return []

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                quote_route_snapshot=quote_routes(instrument),
                server_sleeping=lambda: any(
                    message.get("source") == "quote:stream" and message.get("degraded")
                    for message in websocket.messages
                ),
                screener_bases=fail_bases,
                store_factory=Store,
            ),
        )
    )

    degraded = next(message for message in websocket.messages if message.get("degraded"))
    assert degraded["type"] in {"quote_snapshot", "quote_aux_status"}
    assert degraded["warnings"] == {
        "quote_rows": "",
        "screener_bases": "bases unavailable",
        "option_targets": "targets unavailable",
        "fast_indicators": "",
        "price_alert_runtime": "",
    }


def test_run_quote_stream_skips_runtime_settings_for_providers_without_runtime_settings() -> None:
    instrument = provider_identity("BTC", "coinbase", "BTC-USD")
    websocket = FakeWebSocket()
    sleeping = iter([False, True, True])
    applied = []

    asyncio.run(
        run_quote_stream(
            websocket,
            route_query(instrument),
            "5m",
            _deps(
                apply_provider_runtime_settings_async=lambda: (
                    applied.append(True) or asyncio.sleep(0, result={})
                ),
                server_sleeping=lambda: next(sleeping),
                quote_route_snapshot=quote_routes(instrument),
            ),
        )
    )

    assert applied == []
