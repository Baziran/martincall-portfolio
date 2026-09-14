from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
    MARKET_ANALYSIS_RESEARCH_REFRESH_SECONDS,
    build_market_analysis_snapshot_response,
    decode_market_analysis_cache_snapshot,
    market_analysis_key,
    market_analysis_refresh_seconds,
    market_analysis_requires_periodic_refresh,
    market_analysis_request_identity,
    register_market_analysis_wanted,
    revoke_market_analysis_wanted_locked,
    trim_market_analysis_cache,
)


def _ready_cache(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    return {
        "status": "ready",
        "snapshot_json": raw,
        "size_bytes": len(raw),
    }


def test_market_analysis_key_is_stable() -> None:
    payload = {"symbol": "ES", "interval": "5m", "range": "1d"}
    assert market_analysis_key(payload) == market_analysis_key(dict(payload))


def test_market_analysis_key_ignores_transport_version_but_keeps_inputs() -> None:
    payload = {
        "symbol": "ES",
        "interval": "5m",
        "range": "1d",
        "indicator_params": {"rounded_reversal": {"enabled": True}},
        "market_version": {
            "latest_ts": "2026-08-03T10:00:00+00:00",
            "client": "quote-a",
        },
    }

    assert market_analysis_key(payload) == market_analysis_key(
        {
            **payload,
            "market_version": {
                "latest_ts": "2026-08-03T10:05:00+00:00",
                "client": "quote-b",
            },
        }
    )
    assert market_analysis_key(payload) != market_analysis_key(
        {
            **payload,
            "indicator_params": {
                "rounded_reversal": {"enabled": False},
            },
        }
    )


def test_research_capture_is_a_distinct_periodic_effect_scope() -> None:
    standard = {
        "instrument_id": "ibkr|future_root|ES|CME|USD|",
        "route_fingerprint": "ibkr|future_root|ES|CME|USD||current:11004968",
        "source": "ibkr",
        "interval": "5m",
        "range": "1d",
        "signal_range": "1d",
        "show_visuals": False,
    }
    research = {
        **standard,
        "analysis_effects": MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
    }

    assert market_analysis_key(standard) != market_analysis_key(research)
    assert market_analysis_request_identity(standard) != market_analysis_request_identity(research)
    assert market_analysis_requires_periodic_refresh(research) is True
    assert (
        market_analysis_refresh_seconds(research, default_seconds=10.0)
        == MARKET_ANALYSIS_RESEARCH_REFRESH_SECONDS
    )


def test_trim_market_analysis_cache_evicts_expired_and_overflow() -> None:
    cache: dict[str, dict[str, Any]] = {
        "old": {"updated_monotonic": 0.0},
        "keep": {"updated_monotonic": 100.0},
        "mid": {"updated_monotonic": 50.0},
        "new": {"updated_monotonic": 90.0},
    }
    trim_market_analysis_cache(cache, ttl_seconds=10.0, max_size=2, now=100.0)
    assert "old" not in cache
    assert len(cache) == 2
    assert "keep" in cache
    assert "new" in cache


def test_trim_market_analysis_cache_evicts_over_byte_budget() -> None:
    cache: dict[str, dict[str, Any]] = {
        "old": {**_ready_cache({"value": "old"}), "updated_monotonic": 10.0},
        "mid": {**_ready_cache({"value": "mid"}), "updated_monotonic": 20.0},
        "new": {**_ready_cache({"value": "new"}), "updated_monotonic": 30.0},
    }
    retained_bytes = cache["mid"]["size_bytes"] + cache["new"]["size_bytes"]
    trim_market_analysis_cache(
        cache,
        ttl_seconds=100.0,
        max_size=3,
        max_bytes=retained_bytes,
        now=30.0,
    )
    assert "old" not in cache
    assert "mid" in cache
    assert "new" in cache


def test_register_market_analysis_wanted_dedupes_identity() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        payload = {
            "instrument_id": "ibkr|future_root|ES|CME|USD|",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD||current:11004968",
            "symbol": "ES",
            "interval": "5m",
            "range": "1d",
            "source": "ibkr",
            "signal_range": "3d",
            "show_visuals": True,
        }
        wanted: dict[str, dict[str, Any]] = {
            "old-key": {
                "wanted_at": time.monotonic(),
                "payload": dict(payload),
            }
        }
        cache: dict[str, dict[str, Any]] = {"old-key": _ready_cache({})}
        tasks: dict[str, asyncio.Task[Any]] = {}
        status = await register_market_analysis_wanted(
            "new-key",
            payload,
            lock=lock,
            wanted=wanted,
            cache=cache,
            tasks=tasks,
            request_identity=market_analysis_request_identity,
            cache_ttl_seconds=120.0,
            cache_max_size=64,
        )
        assert status == "queued"
        assert "old-key" not in wanted
        assert "old-key" not in cache
        assert "new-key" in wanted

    asyncio.run(run())


def test_gex_analysis_lanes_do_not_supersede_each_other() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        base_payload = {
            "instrument_id": "ibkr|future_root|ES|CME|USD|",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD||current:11004968",
            "interval": "5m",
            "range": "1d",
            "source": "ibkr",
            "signal_range": "3d",
            "show_visuals": True,
        }
        request_payload = {
            **base_payload,
            "gex_context_active": False,
            "gex_capture_mode": "request",
        }
        live_payload = {
            **base_payload,
            "gex_context_active": True,
            "gex_capture_mode": "live",
        }
        wanted: dict[str, dict[str, Any]] = {
            "request-key": {
                "wanted_at": time.monotonic(),
                "payload": request_payload,
            }
        }
        cache: dict[str, dict[str, Any]] = {
            "request-key": {
                **_ready_cache({}),
                "updated_monotonic": time.monotonic(),
            }
        }
        tasks: dict[str, asyncio.Task[Any]] = {}

        assert market_analysis_request_identity(
            request_payload
        ) != market_analysis_request_identity(live_payload)
        status = await register_market_analysis_wanted(
            "live-key",
            live_payload,
            lock=lock,
            wanted=wanted,
            cache=cache,
            tasks=tasks,
            request_identity=market_analysis_request_identity,
            cache_ttl_seconds=120.0,
            cache_max_size=64,
        )

        assert status == "queued"
        assert set(wanted) == {"request-key", "live-key"}
        assert cache["request-key"]["status"] == "ready"

    asyncio.run(run())


def test_register_market_analysis_wanted_cancels_superseded_running_task() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        payload = {
            "instrument_id": "ibkr|future_root|ES|CME|USD|",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD||current:11004968",
            "symbol": "ES",
            "interval": "5m",
            "range": "1d",
            "source": "ibkr",
            "signal_range": "3d",
            "show_visuals": True,
        }
        wanted: dict[str, dict[str, Any]] = {
            "old-key": {
                "wanted_at": time.monotonic(),
                "payload": dict(payload),
            }
        }
        cache: dict[str, dict[str, Any]] = {}

        async def old_job() -> None:
            await asyncio.Event().wait()

        old_task = asyncio.create_task(old_job())
        tasks: dict[str, asyncio.Task[Any]] = {"old-key": old_task}
        identities: dict[str, tuple[Any, ...]] = {
            "old-key": market_analysis_request_identity(payload)
        }

        status = await register_market_analysis_wanted(
            "new-key",
            payload,
            lock=lock,
            wanted=wanted,
            cache=cache,
            tasks=tasks,
            identities=identities,
            request_identity=market_analysis_request_identity,
            cache_ttl_seconds=120.0,
            cache_max_size=64,
        )
        await asyncio.sleep(0)

        assert status == "queued"
        assert old_task.cancelled()
        assert tasks["old-key"] is old_task
        assert "old-key" in identities
        async with lock:
            revoke_market_analysis_wanted_locked(
                "old-key",
                wanted=wanted,
                tasks=tasks,
                identities=identities,
            )
        assert "old-key" not in tasks
        assert "old-key" not in identities
        assert "new-key" in wanted

    asyncio.run(run())


def test_market_analysis_snapshot_missing_key() -> None:
    payload = build_market_analysis_snapshot_response("", cached=None, task_running=False)
    assert payload["ok"] is False
    assert payload["status"] == "missing"
    assert payload["error"]["code"] == "MARKET_ANALYSIS_KEY_MISSING"


def test_market_analysis_snapshot_running() -> None:
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached=None,
        task_running=True,
        instrument_id="coinbase|contract|BTC-USD",
        route_fingerprint="coinbase|contract|BTC-USD",
    )
    assert payload["ok"] is False
    assert payload["status"] == "running"
    assert payload["analysis_key"] == "abc123"
    assert payload["error"]["code"] == "MARKET_ANALYSIS_RUNNING"
    assert payload["error"]["retryable"] is True
    assert payload["instrument_id"] == "coinbase|contract|BTC-USD"
    assert payload["route_fingerprint"] == "coinbase|contract|BTC-USD"


def test_market_analysis_snapshot_queued() -> None:
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached=None,
        task_running=False,
        task_queued=True,
        instrument_id="coinbase|contract|BTC-USD",
        route_fingerprint="coinbase|contract|BTC-USD",
    )
    assert payload["ok"] is False
    assert payload["status"] == "queued"
    assert payload["analysis_key"] == "abc123"
    assert payload["error"]["code"] == "MARKET_ANALYSIS_QUEUED"
    assert payload["error"]["retryable"] is True
    assert payload["instrument_id"] == "coinbase|contract|BTC-USD"
    assert payload["route_fingerprint"] == "coinbase|contract|BTC-USD"


def test_market_analysis_snapshot_not_found() -> None:
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached=None,
        task_running=False,
        instrument_id="coinbase|contract|BTC-USD",
        route_fingerprint="coinbase|contract|BTC-USD",
    )
    assert payload["ok"] is False
    assert payload["status"] == "missing"
    assert payload["error"]["code"] == "MARKET_ANALYSIS_NOT_FOUND"
    assert payload["instrument_id"] == "coinbase|contract|BTC-USD"
    assert payload["route_fingerprint"] == "coinbase|contract|BTC-USD"


def test_market_analysis_snapshot_ready() -> None:
    snapshot = {"ok": True, "symbol": "ES"}
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached=_ready_cache(snapshot),
        task_running=False,
    )
    assert json.loads(payload.body) == snapshot


def test_market_analysis_snapshot_projects_selected_vsa_hours_without_mutating_cache() -> None:
    snapshot = {
        "ok": True,
        "vsa_volume": {
            "latest": {"ts": "2026-01-01T14:00:00+00:00"},
            "series": [
                {"ts": "2026-01-01T11:59:00+00:00"},
                {"ts": "2026-01-01T12:01:00+00:00"},
                {"ts": "2026-01-01T14:00:00+00:00"},
            ],
            "events": [],
            "overlays": [],
            "status": {},
        },
    }

    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached=_ready_cache(snapshot),
        task_running=False,
        vsa_render_hours=2,
    )
    projected = json.loads(payload.body)

    assert [row["ts"] for row in projected["vsa_volume"]["series"]] == [
        "2026-01-01T12:01:00+00:00",
        "2026-01-01T14:00:00+00:00",
    ]
    assert projected["vsa_volume"]["status"]["render_window_seconds"] == 7200
    assert len(snapshot["vsa_volume"]["series"]) == 3


def test_market_analysis_snapshot_ready_json_bytes() -> None:
    raw = b'{"ok":true,"symbol":"ES"}'
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached={"status": "ready", "snapshot_json": raw, "size_bytes": len(raw)},
        task_running=False,
    )
    assert payload.media_type == "application/json"
    assert payload.body == raw


def test_market_analysis_cache_decoder_rejects_parallel_or_untracked_payloads() -> None:
    with pytest.raises(TypeError, match="snapshot bytes"):
        decode_market_analysis_cache_snapshot(
            {"status": "ready", "snapshot": {"ok": True}, "size_bytes": 0}
        )
    with pytest.raises(ValueError, match="byte size"):
        decode_market_analysis_cache_snapshot(
            {"status": "ready", "snapshot_json": b"{}", "size_bytes": 1}
        )


def test_market_analysis_snapshot_error() -> None:
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached={"status": "error", "message": "boom"},
        task_running=False,
        instrument_id="coinbase|contract|BTC-USD",
        route_fingerprint="coinbase|contract|BTC-USD",
    )
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "MARKET_ANALYSIS_FAILED"
    assert payload["message"] == "boom"
    assert payload["analysis_key"] == "abc123"
    assert payload["instrument_id"] == "coinbase|contract|BTC-USD"
    assert payload["route_fingerprint"] == "coinbase|contract|BTC-USD"


def test_market_analysis_snapshot_preserves_terminal_worker_error_contract() -> None:
    payload = build_market_analysis_snapshot_response(
        "abc123",
        cached={
            "status": "error",
            "message": "application restart required",
            "error_code": "MARKET_ANALYSIS_WORKER_UNAVAILABLE",
            "retryable": False,
        },
        task_running=False,
        instrument_id="coinbase|contract|BTC-USD",
        route_fingerprint="coinbase|contract|BTC-USD",
    )

    assert payload["error"] == {
        "code": "MARKET_ANALYSIS_WORKER_UNAVAILABLE",
        "category": "analysis",
        "retryable": False,
        "message": "application restart required",
    }
