from __future__ import annotations

import asyncio
import inspect
import threading
from copy import deepcopy

import pytest

from aef_terminal import config as app_config
from aef_terminal.indicators.modules.ai_third_opinion import providers
from aef_terminal.indicators.modules.ai_third_opinion import router as ai_router
from aef_terminal.indicators.modules.ai_third_opinion import service as advisor


@pytest.fixture(autouse=True)
def clear_ai_third_opinion_runtime_state():
    advisor._VERDICT_CACHE.clear()
    providers._PROVIDER_BACKOFF.clear()
    yield
    advisor._VERDICT_CACHE.clear()
    providers._PROVIDER_BACKOFF.clear()


def test_ai_third_opinion_connector_contract_has_no_local_degraded_status() -> None:
    source = inspect.getsource(advisor)
    assert "local_" + "fallback" not in source
    assert "fallback_" + "timeframe" not in source
    assert "fallback_" + "data_quality" not in source
    assert 'status.pop("state", None)' not in source
    assert 'status.pop("message", None)' not in source
    assert 'status_payload.pop("state", None)' not in source
    assert 'status_payload.pop("message", None)' not in source


def test_ai_discussion_cancellation_settles_physical_provider_call(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_discussion(*_args, **_kwargs) -> dict:
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return {"ok": True}

    monkeypatch.setattr(ai_router, "discuss_ai_third_opinion", blocking_discussion)
    endpoint = next(
        route.endpoint
        for route in ai_router.create_router().routes
        if route.path == "/api/ai-third-opinion/discuss"
    )

    async def scenario() -> None:
        task = asyncio.create_task(endpoint({"snapshot": {}, "question": "why?"}))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() is True

    asyncio.run(scenario())


def _snapshot() -> dict:
    return {
        "meta": {
            "instrument_id": "ibkr|future_root|ES",
            "route_fingerprint": "ibkr|future|ESM6",
            "symbol": "ES",
            "provider_symbol": "ES",
            "timeframe": "5m",
            "source": "ibkr",
            "analysis_ts": "2026-01-01T15:00:00+00:00",
            "confirmed_bar_count": 2,
            "data_quality": {"signals_ok": True},
        },
        "bars": [
            {
                "ts": "2026-01-01T14:55:00+00:00",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 1000,
                "closed": True,
                "state": "confirmed",
            },
            {
                "ts": "2026-01-01T15:00:00+00:00",
                "open": 100.5,
                "high": 102,
                "low": 100,
                "close": 101.5,
                "volume": 1200,
                "closed": True,
                "state": "confirmed",
            },
        ],
        "decision": {
            "kind": "transit",
            "direction": "long",
            "confidence": 0.7,
            "action": "WATCH",
            "reasons": ["test"],
        },
        "direction_sentiment": {"direction": "long", "score": 62},
        "indicators": {
            "channel_master": {
                "active": True,
                "count": 1,
                "analysis_ts": "2026-01-01T15:00:00+00:00",
                "price": 101.5,
                "nearest": {
                    "channel_id": "ch_1",
                    "label": "HIGH",
                    "role": "upper_edge",
                    "price": 102.0,
                    "distance_atr": 0.18,
                    "side": "below",
                    "touch": True,
                    "reclaim": False,
                    "inside": True,
                    "direction": "short",
                    "primary_path": "bounce",
                    "no_fade": False,
                    "break_score": 38.0,
                    "bounce_score": 62.0,
                    "raw_break_score": 44.0,
                    "target": 100.0,
                },
                "channels": [
                    {
                        "id": "ch_1",
                        "inside": True,
                        "position_pct": 0.82,
                        "lower": 100.0,
                        "upper": 102.0,
                        "base": 100.0,
                        "offset": 2.0,
                        "slope_per_confirmed_bar": 0.02,
                        "impulse_strength": 0.4,
                        "levels": [
                            {
                                "label": "HIGH",
                                "role": "upper_edge",
                                "price": 102.0,
                                "distance_atr": 0.18,
                                "side": "below",
                                "touch": True,
                                "reclaim": False,
                                "inside": True,
                                "direction": "short",
                                "primary_path": "bounce",
                                "no_fade": False,
                                "break_score": 38.0,
                                "bounce_score": 62.0,
                                "raw_break_score": 44.0,
                            },
                            {
                                "label": "MID",
                                "role": "midline",
                                "price": 101.0,
                                "distance_atr": 0.35,
                                "side": "above",
                                "touch": False,
                                "reclaim": False,
                                "inside": True,
                                "direction": "long",
                                "primary_path": "break",
                                "no_fade": True,
                                "break_score": 73.0,
                                "bounce_score": 27.0,
                                "raw_break_score": 79.0,
                            },
                        ],
                    }
                ],
            },
            "ai_third_opinion": {
                "latest": {
                    "phase": "trend_up",
                    "bias": "bullish",
                    "market_view": "long",
                    "headline": "Look for long context: early trend formation",
                    "pattern": "early trend formation",
                    "confidence": 0.55,
                    "timeframe": "5m",
                    "data_quality": "confirmed",
                    "connector": {
                        "provider": "openai",
                        "status": "pending_external_worker",
                        "authoritative": False,
                        "external_requested": True,
                        "channel_context_requested": True,
                    },
                    "evidence": ["local"],
                    "contradictions": [],
                },
                "overlays": [],
                "status": {
                    "id": "ai_third_opinion",
                    "state_code": "no_signal",
                    "health": "ok",
                    "reason_code": "no_current_signal",
                    "trigger_event": {"code": "indicator_no_signal"},
                    "mode": "confirmed",
                    "bar_count": 2,
                    "calculated_at": "now",
                },
            },
            "market_spotlight": {"latest": {"state": "TREND", "direction": "long"}},
        },
    }


def test_ai_third_opinion_marks_missing_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_PROVIDER", raising=False)
    snapshot = _snapshot()

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert indicator["latest"]["connector"]["status"] == "missing_api_key"
    assert indicator["latest"]["connector"]["health"] == {
        "state": "missing_api_key",
        "label": "NO KEY",
        "detail": "env",
    }
    assert indicator["latest"]["connector"]["authoritative"] is False
    assert "OPENAI_API_KEY" in indicator["latest"]["connector"]["message"]
    assert indicator["table"]["columns"][3]["cells"] == ["AI", "OFF", "no key"]
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "NO KEY", "env"]


def test_ai_third_opinion_manual_refresh_does_not_auto_request(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("manual refresh should not auto request model")

    monkeypatch.setattr(providers, "_request_openai_verdict", fake_request)
    snapshot = _snapshot()
    connector = snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]
    connector["status"] = "manual_refresh"
    connector["external_requested"] = False
    connector["external_enabled"] = True
    connector["refresh_mode"] = "manual"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    assert calls == []
    assert (
        snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["status"]
        == "manual_refresh"
    )


def test_ai_third_opinion_marks_insufficient_quota_health(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("AI_THIRD_OPINION_PROVIDER", raising=False)
    advisor._VERDICT_CACHE.clear()

    def fake_request(*args, **kwargs):
        raise providers.AiProviderError(
            provider="openai",
            code="openai_http_429",
            status="insufficient_quota",
            message="You exceeded your current quota",
            retryable=False,
        )

    monkeypatch.setattr(providers, "_request_openai_verdict", fake_request)
    snapshot = _snapshot()

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert indicator["latest"]["connector"]["status"] == "insufficient_quota"
    assert indicator["latest"]["connector"]["health"] == {
        "state": "insufficient_quota",
        "label": "NO $",
        "detail": "quota",
    }
    assert indicator["table"]["columns"][3]["cells"] == ["AI", "ERR", "stale"]
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "NO $", "quota"]
    assert indicator["overlays"][-1]["table"]["columns"][4]["cells"] == ["HLTH", "NO $", "quota"]


def test_ai_third_opinion_extracts_response_token_usage() -> None:
    usage = providers._token_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 25,
                "total_tokens": 125,
                "input_tokens_details": {"cached_tokens": 12},
            }
        }
    )

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "input_tokens_details": {"cached_tokens": 12},
    }

    chat_usage = providers._token_usage(
        {"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
    )
    assert chat_usage == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    gemini_usage = providers._token_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 13,
                "candidatesTokenCount": 9,
                "totalTokenCount": 22,
            }
        }
    )
    assert gemini_usage == {"input_tokens": 13, "output_tokens": 9, "total_tokens": 22}
    gemini_interactions_usage = providers._token_usage(
        {"usage": {"total_tokens": 23, "total_input_tokens": 6, "total_output_tokens": 1}}
    )
    assert gemini_interactions_usage == {"input_tokens": 6, "output_tokens": 1, "total_tokens": 23}
    ollama_usage = providers._token_usage({"usage": {"prompt_eval_count": 21, "eval_count": 8}})
    assert ollama_usage == {"input_tokens": 21, "output_tokens": 8, "total_tokens": 29}


def test_ai_third_opinion_extracts_gemini_interactions_text() -> None:
    payload = {
        "steps": [
            {"type": "thought"},
            {"type": "model_output", "content": [{"type": "text", "text": "OK"}]},
        ]
    }

    assert providers._extract_gemini_text(payload) == "OK"


def test_provider_defaults_do_not_reuse_openai_model_env(monkeypatch) -> None:
    monkeypatch.setenv("AI_THIRD_OPINION_MODEL", "gpt-openai-custom")
    monkeypatch.delenv("AI_THIRD_OPINION_DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_CODEX_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("CODEX_ADVISOR_MODEL", raising=False)

    assert providers.model_name("openai") == "gpt-openai-custom"
    assert providers.model_name("deepseek") == "deepseek-v4-flash"
    assert providers.model_name("gemini") == "gemini-2.5-flash"
    assert providers.model_name("ollama") == "llama3.2"
    assert providers.model_name("codex") == "gpt-5.6-terra"


def test_ollama_model_aliases_map_to_local_tags(monkeypatch) -> None:
    monkeypatch.delenv("AI_THIRD_OPINION_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    snapshot = _snapshot()
    connector = snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]
    connector["provider"] = "ollama"
    connector["model"] = "deepseek-r1"

    assert (
        providers.model_name("ollama", snapshot["indicators"]["ai_third_opinion"])
        == "deepseek-r1:8b"
    )

    connector["model"] = "qwen2.5"
    assert (
        providers.model_name("ollama", snapshot["indicators"]["ai_third_opinion"])
        == "qwen2.5-coder:14b"
    )

    connector["model"] = "deepseek-r1:14b"
    assert (
        providers.model_name("ollama", snapshot["indicators"]["ai_third_opinion"])
        == "deepseek-r1:14b"
    )


def test_ai_prompts_require_numeric_channel_comparisons() -> None:
    verdict_prompt = providers._verdict_system_prompt()
    discussion_prompt = providers._discussion_system_prompt()

    for prompt in (verdict_prompt, discussion_prompt):
        assert "Compare numeric prices explicitly" in prompt
        assert "do not infer direction from a channel label" in prompt
        assert "relation_to_price" in prompt
        assert "distance_points" in prompt
        assert "ghost_low" in prompt
        assert "primary_path, no_fade, break_score, bounce_score, and raw_break_score" in prompt
        assert "deterministic backend facts" in prompt
        assert "when no_fade is true, do not propose a channel fade" in prompt
        assert "unless a later confirmed reclaim is supplied" in prompt
    assert "separately typed provisional live_bar" in discussion_prompt
    assert (
        "higher_context derived only from those confirmed base-timeframe bars" in discussion_prompt
    )
    assert "Return object_suggestions only when the user explicitly asks" in discussion_prompt


def test_gemini_verdict_body_uses_single_input_message() -> None:
    body = providers._gemini_verdict_body(
        "gemini-3.5-flash",
        {
            "meta": {"symbol": "ES"},
            "bars": [],
            "candle_summary": {"count": 0},
            "manual_channels": {"enabled": False},
        },
    )

    assert body["model"] == "gemini-3.5-flash"
    assert isinstance(body["input"], str)
    assert "messages" not in body
    assert body["input"].count('"manual_channels"') == 1
    assert body["response_format"]["mime_type"] == "application/json"


def test_ai_third_opinion_compacts_context_window(monkeypatch) -> None:
    monkeypatch.setenv("AI_THIRD_OPINION_CONTEXT_BARS", "16")
    snapshot = _snapshot()
    snapshot["bars"] = [
        {
            "ts": f"2026-01-01T15:{index:02d}:00+00:00",
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100.5 + index,
            "volume": 1000 + index,
            "closed": True,
            "source": "ibkr",
            "timeframe": "5m",
        }
        for index in range(30)
    ]

    payload = advisor._advisor_input(snapshot, snapshot["indicators"]["ai_third_opinion"])

    assert set(payload) == {"meta", "bars", "candle_summary", "manual_channels"}
    assert len(payload["bars"]) == 16
    assert payload["bars"][0]["t"] == "15:14:00Z"
    assert payload["bars"][-1]["t"] == "15:29:00Z"
    assert set(payload["bars"][0]) == {"t", "o", "h", "l", "c", "v"}
    assert "source" not in payload["bars"][0]
    assert "timeframe" not in payload["bars"][0]
    assert payload["candle_summary"]["count"] == 16
    assert payload["candle_summary"]["last_close"] == 129.5


def test_ai_third_opinion_context_window_defaults_by_provider(monkeypatch) -> None:
    monkeypatch.delenv("AI_THIRD_OPINION_CONTEXT_BARS", raising=False)
    snapshot = _snapshot()
    snapshot["bars"] = [
        {
            "ts": f"2026-01-01T15:{index % 60:02d}:00+00:00",
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100.5 + index,
            "volume": 1000 + index,
            "closed": True,
        }
        for index in range(200)
    ]
    indicator = snapshot["indicators"]["ai_third_opinion"]

    external_payload = advisor._advisor_input(snapshot, indicator)
    assert len(external_payload["bars"]) == 100
    assert external_payload["candle_summary"]["count"] == 100

    indicator["latest"]["connector"]["provider"] = "ollama"
    ollama_payload = advisor._advisor_input(snapshot, indicator)
    assert len(ollama_payload["bars"]) == 100
    assert ollama_payload["candle_summary"]["count"] == 100

    monkeypatch.setenv("AI_THIRD_OPINION_CONTEXT_BARS", "80")
    assert len(advisor._advisor_input(snapshot, indicator)["bars"]) == 80

    indicator["latest"]["connector"]["provider"] = "openai"
    assert len(advisor._advisor_input(snapshot, indicator)["bars"]) == 80


def test_manual_channel_levels_include_deterministic_price_relation(monkeypatch) -> None:
    monkeypatch.delenv("AI_THIRD_OPINION_CONTEXT_BARS", raising=False)
    snapshot = _snapshot()
    snapshot["bars"][-1].update({"open": 7423.5, "high": 7426.0, "low": 7422.0, "close": 7424.0})
    channel_master = snapshot["indicators"]["channel_master"]
    channel_master["price"] = 7445.25
    channel_master["nearest"]["price"] = 7473.689
    channel_master["channels"][0]["levels"] = [
        {"label": "PROJ G-1", "role": "ghost_low", "price": 7473.689, "distance_atr": 0.2},
        {"label": "PROJ G-0.5", "role": "ghost_low", "price": 7490.4787, "distance_atr": 0.4},
        {"label": "BELOW", "role": "lower_edge", "price": 7420.0, "distance_atr": 0.5},
    ]

    payload = advisor._advisor_input(snapshot, snapshot["indicators"]["ai_third_opinion"])
    nearest = payload["manual_channels"]["nearest"]
    levels = payload["manual_channels"]["channels"][0]["nearest_levels"]

    assert payload["manual_channels"]["price"] == 7424.0
    assert payload["manual_channels"]["price_ts"] == "2026-01-01T15:00:00+00:00"
    assert payload["manual_channels"]["distance_reference"] == "last_confirmed_close"
    assert payload["manual_channels"]["channel_observed_price"] == 7445.25
    assert nearest["relation_to_price"] == "above"
    assert nearest["distance_points"] == 49.689
    assert levels[0]["label"] == "PROJ G-1"
    assert levels[0]["relation_to_price"] == "above"
    assert levels[0]["distance_points"] == 49.689
    assert levels[2]["label"] == "BELOW"
    assert levels[2]["relation_to_price"] == "below"
    assert levels[2]["distance_points"] == -4.0


def test_discussion_keeps_live_bar_separate_from_100_confirmed_bars(monkeypatch) -> None:
    monkeypatch.delenv("AI_THIRD_OPINION_CONTEXT_BARS", raising=False)
    snapshot = _snapshot()
    snapshot["bars"] = [
        {
            "ts": f"2026-01-01T{7 + index // 12:02d}:{(index % 12) * 5:02d}:00+00:00",
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100.5 + index,
            "volume": 1000 + index,
            "closed": True,
            "state": "confirmed",
            "authoritative": True,
        }
        for index in range(105)
    ]
    snapshot["bars"].append(
        {
            "ts": "2026-01-01T15:45:00+00:00",
            "expected_close": "2026-01-01T15:50:00+00:00",
            "open": 205.0,
            "high": 206.0,
            "low": 204.5,
            "close": 205.75,
            "volume": 31,
            "closed": False,
            "state": "forming",
            "source": "ibkr-live",
        }
    )

    payload = advisor._advisor_input(
        snapshot,
        snapshot["indicators"]["ai_third_opinion"],
        include_live_bar=True,
    )

    assert len(payload["bars"]) == 100
    assert payload["candle_summary"]["count"] == 100
    assert payload["bars"][-1]["c"] == 204.5
    assert payload["live_bar"]["provisional"] is True
    assert payload["live_bar"]["authoritative"] is False
    assert payload["live_bar"]["is_confirmed"] is False
    assert payload["live_bar"]["current_price"] == 205.75
    assert payload["live_bar"]["next_close_at"] == "2026-01-01T15:50:00+00:00"
    assert payload["live_bar"]["ohlcv"]["c"] == 205.75
    assert payload["higher_context"]["source"] == "confirmed_base_bars"
    assert payload["higher_context"]["synthetic_bars"] is False
    assert payload["higher_context"]["confirmed_bar_count"] == 100
    assert payload["higher_context"]["short_window"]["count"] == 20
    assert payload["higher_context"]["medium_window"]["count"] == 50
    assert payload["higher_context"]["full_window"]["count"] == 100


def test_ai_third_opinion_merges_openai_verdict_and_uses_cache(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("AI_THIRD_OPINION_PROVIDER", raising=False)
    advisor._VERDICT_CACHE.clear()
    calls = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        calls.append((advisor_input, api_key, model, timeout_seconds))
        return {
            "phase": "compression",
            "bias": "neutral",
            "market_view": "wait",
            "headline": "Wait: compression before expansion",
            "pattern": "compression before expansion",
            "confidence": 0.72,
            "timeframe": "5m",
            "evidence": ["range is tightening", "channel context is mixed"],
            "contradictions": ["trend vote is still long"],
            "invalidation": ["acceptance above range high"],
            "data_quality": "confirmed",
            "_openai": {
                "response_id": "resp_123",
                "model": "gpt-test",
                "endpoint": "responses",
                "latency_ms": 42.0,
                "usage": {"input_tokens": 321, "output_tokens": 87, "total_tokens": 408},
            },
        }

    monkeypatch.setattr(providers, "_request_openai_verdict", fake_request)
    first = _snapshot()
    second = deepcopy(first)
    second["meta"]["analysis_ts"] = "2026-01-01T15:00:05+00:00"
    second["meta"]["freshness"] = {"seconds": 5}

    advisor.enrich_snapshot_with_ai_third_opinion(first)
    advisor.enrich_snapshot_with_ai_third_opinion(second)

    assert len(calls) == 1
    assert set(calls[0][0]) == {"meta", "bars", "candle_summary", "manual_channels"}
    assert "analysis_ts" not in calls[0][0]["meta"]
    assert "freshness" not in calls[0][0]["meta"]
    assert "data_quality" not in calls[0][0]["meta"]
    assert "local_opinion" not in calls[0][0]
    assert "upstream" not in calls[0][0]
    assert "decision" not in calls[0][0]
    assert "direction_sentiment" not in calls[0][0]
    assert calls[0][0]["manual_channels"]["enabled"] is True
    assert calls[0][0]["candle_summary"]["count"] == 2
    channel_context = calls[0][0]["manual_channels"]
    assert channel_context["channel_observed_ts"] == "2026-01-01T15:00:00+00:00"
    assert channel_context["nearest"]["role"] == "upper_edge"
    assert channel_context["nearest"]["primary_path"] == "bounce"
    assert channel_context["nearest"]["no_fade"] is False
    assert channel_context["nearest"]["break_score"] == 38.0
    assert channel_context["nearest"]["bounce_score"] == 62.0
    assert channel_context["nearest"]["raw_break_score"] == 44.0
    levels = channel_context["channels"][0]["nearest_levels"]
    assert levels[0]["label"] == "HIGH"
    assert levels[0]["primary_path"] == "bounce"
    assert levels[0]["no_fade"] is False
    assert levels[1]["label"] == "MID"
    assert levels[1]["primary_path"] == "break"
    assert levels[1]["no_fade"] is True
    first_indicator = first["indicators"]["ai_third_opinion"]
    second_indicator = second["indicators"]["ai_third_opinion"]
    assert first_indicator["latest"]["phase"] == "compression"
    assert first_indicator["latest"]["market_view"] == "wait"
    assert first_indicator["latest"]["headline"] == "Wait: compression before expansion"
    assert first_indicator["latest"]["pattern"] == "compression before expansion"
    assert "tooltip" not in first_indicator["latest"]
    assert first_indicator["latest"]["scenario"] == "ai_third_opinion"
    assert first_indicator["latest"]["trigger_event"] == {
        "code": "ai_advisory",
        "phase": "compression",
        "market_view": "wait",
    }
    assert "context_lines" not in first_indicator["latest"]
    assert first_indicator["latest"]["metrics"]["pattern"] == "compression before expansion"
    assert first_indicator["latest"]["fact_groups"][0]["items"][0] == {
        "code": "ai_evidence_item",
        "content": "range is tightening",
    }
    assert first_indicator["latest"]["source"] == "openai"
    assert first_indicator["latest"]["connector"]["provider"] == "openai"
    assert first_indicator["latest"]["connector"]["status"] == "ready"
    assert first_indicator["latest"]["connector"]["endpoint"] == "responses"
    assert first_indicator["latest"]["connector"]["response_id"] == "resp_123"
    assert first_indicator["latest"]["connector"]["usage"] == {
        "input_tokens": 321,
        "output_tokens": 87,
        "total_tokens": 408,
    }
    assert first_indicator["latest"]["connector"]["health"] == {
        "state": "connected",
        "label": "OK",
        "detail": "live",
    }
    assert first_indicator["latest"]["connector"]["channel_context_requested"] is True
    assert first_indicator["latest"]["connector"]["response_bars_ago"] == 0
    assert first_indicator["latest"]["signal"]["blocked_reason"] == "advisory_context_only"
    assert first_indicator["latest"]["signal"]["reason_code"] == "advisory_context_only"
    assert "reason" not in first_indicator["latest"]["signal"]
    assert first_indicator["table"]["columns"][0]["cells"] == ["VIEW", "WAIT", "compression"]
    assert first_indicator["table"]["columns"][2]["cells"] == [
        "WHY",
        "compression before expansion",
        "NEUTRAL",
    ]
    assert first_indicator["table"]["ai_status"] == "ready"
    assert first_indicator["table"]["ai_thinking"] is False
    assert first_indicator["table"]["columns"][3]["cells"] == [
        "AI",
        "ON",
        {"kind": "integer", "value": 0, "suffix": " bars"},
    ]
    assert first_indicator["table"]["columns"][4]["cells"] == ["HLTH", "OK", "live"]
    assert first_indicator["table"]["columns"][5]["cells"] == [
        "TOK",
        {"kind": "integer", "value": 408, "suffix": " tok"},
        {
            "parts": (
                {"kind": "integer", "value": 321, "empty": "-"},
                {"kind": "integer", "value": 87, "empty": "-"},
            ),
            "separator": "/",
        },
    ]
    assert first_indicator["table"]["columns"][-1]["cells"] == ["ASK", "SEND", "context"]
    assert first_indicator["table"]["columns"][-1]["action"] == "ai-third-opinion-discuss-send"
    assert second_indicator["latest"]["connector"]["status"] == "cache"
    assert second_indicator["latest"]["connector"]["health"] == {
        "state": "cache",
        "label": "CACHE",
        "detail": "prev",
    }
    assert second_indicator["latest"]["connector"]["cache_hit"] is True
    assert second_indicator["table"]["ai_thinking"] is False
    assert second_indicator["table"]["columns"][3]["cells"] == [
        "AI",
        "CACHE",
        {"kind": "integer", "value": 0, "suffix": " bars"},
    ]
    assert second_indicator["table"]["columns"][4]["cells"] == ["HLTH", "CACHE", "prev"]


def test_ai_third_opinion_external_contrarian_lens_preserves_raw_ai_view(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    advisor._VERDICT_CACHE.clear()

    def fake_request(_advisor_input, *, api_key, model, timeout_seconds):
        return {
            "phase": "trend_up",
            "bias": "bullish",
            "market_view": "long",
            "headline": "Look for long context: early trend formation",
            "pattern": "early trend formation",
            "confidence": 0.68,
            "timeframe": "5m",
            "evidence": ["trend accepted higher"],
            "contradictions": [],
            "invalidation": ["acceptance below range mid"],
            "data_quality": "confirmed",
            "_openai": {
                "response_id": "resp_contra",
                "model": "gpt-test",
                "endpoint": "responses",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        }

    monkeypatch.setattr(providers, "_request_openai_verdict", fake_request)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["lens"] = "contrarian"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert indicator["latest"]["ai_market_view"] == "long"
    assert indicator["latest"]["market_view"] == "short"
    assert indicator["latest"]["lens"] == "contrarian"
    assert indicator["latest"]["connector"]["lens"] == "contrarian"
    assert indicator["latest"]["signal"]["direction"] == "short"
    assert indicator["table"]["columns"][0]["cells"] == ["VIEW", "SHORT", "AI LONG->SHORT"]
    assert "tooltip" not in indicator["latest"]
    assert "context_lines" not in indicator["latest"]
    assert indicator["latest"]["metrics"]["raw_ai_view"] == "LONG"
    assert indicator["latest"]["metrics"]["displayed_view"] == "SHORT"
    assert any(
        fact["code"].startswith("trap_watch_inside_user_channels")
        for fact in indicator["latest"]["evidence"]["context"]
    )


def test_ai_third_opinion_routes_deepseek_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    advisor._VERDICT_CACHE.clear()
    calls = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        calls.append((advisor_input, api_key, model, timeout_seconds))
        return {
            "phase": "trend_down",
            "bias": "bearish",
            "market_view": "short",
            "headline": "Short context: channel rejection",
            "pattern": "channel rejection",
            "confidence": 0.68,
            "timeframe": "5m",
            "evidence": ["upper channel rejection"],
            "contradictions": [],
            "invalidation": ["reclaim above upper edge"],
            "data_quality": "confirmed",
            "_openai": {
                "provider": "deepseek",
                "response_id": "ds_1",
                "model": "deepseek-v4-pro",
                "endpoint": "chat/completions",
                "latency_ms": 31.0,
                "usage": {"input_tokens": 44, "output_tokens": 12, "total_tokens": 56},
            },
        }

    monkeypatch.setattr(providers, "_request_deepseek_verdict", fake_request)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["provider"] = "deepseek"
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["model"] = "deepseek-v4-pro"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert calls[0][1] == "deepseek-key"
    assert calls[0][2] == "deepseek-v4-pro"
    assert indicator["latest"]["source"] == "deepseek"
    assert indicator["latest"]["connector"]["provider"] == "deepseek"
    assert indicator["latest"]["connector"]["model"] == "deepseek-v4-pro"
    assert indicator["latest"]["connector"]["endpoint"] == "chat/completions"
    assert indicator["latest"]["market_view"] == "short"
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "OK", "live"]


def test_ai_third_opinion_marks_deepseek_balance_as_no_money(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    advisor._VERDICT_CACHE.clear()

    def fake_request(*args, **kwargs):
        raise providers.AiProviderError(
            provider="deepseek",
            code="deepseek_http_402",
            status="insufficient_quota",
            message="You have run out of balance.",
            retryable=False,
        )

    monkeypatch.setattr(providers, "_request_deepseek_verdict", fake_request)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["provider"] = "deepseek"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert indicator["latest"]["connector"]["status"] == "insufficient_quota"
    assert indicator["latest"]["connector"]["provider"] == "deepseek"
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "NO $", "quota"]


def test_ai_third_opinion_routes_gemini_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    advisor._VERDICT_CACHE.clear()
    calls = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        calls.append((advisor_input, api_key, model, timeout_seconds))
        return {
            "phase": "range",
            "bias": "neutral",
            "market_view": "wait",
            "headline": "Wait: range rejection",
            "pattern": "range rejection",
            "confidence": 0.64,
            "timeframe": "5m",
            "evidence": ["manual channel midpoint is holding"],
            "contradictions": [],
            "invalidation": ["acceptance outside channel"],
            "data_quality": "confirmed",
            "_openai": {
                "provider": "gemini",
                "response_id": "gemini_1",
                "model": "gemini-3.5-flash",
                "endpoint": "interactions",
                "latency_ms": 29.0,
                "usage": {"input_tokens": 50, "output_tokens": 15, "total_tokens": 65},
            },
        }

    monkeypatch.setattr(providers, "_request_gemini_verdict", fake_request)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["provider"] = "gemini"
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["model"] = "gemini-3.5-flash"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert calls[0][1] == "gemini-key"
    assert calls[0][2] == "gemini-3.5-flash"
    assert indicator["latest"]["source"] == "gemini"
    assert indicator["latest"]["connector"]["provider"] == "gemini"
    assert indicator["latest"]["connector"]["model"] == "gemini-3.5-flash"
    assert indicator["latest"]["connector"]["endpoint"] == "interactions"
    assert indicator["latest"]["market_view"] == "wait"
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "OK", "live"]


def test_ai_third_opinion_marks_gemini_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["provider"] = "gemini"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert indicator["latest"]["connector"]["status"] == "missing_api_key"
    assert indicator["latest"]["connector"]["provider"] == "gemini"
    assert "GEMINI_API_KEY" in indicator["latest"]["connector"]["message"]
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "NO KEY", "env"]


def test_ai_third_opinion_routes_ollama_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AI_THIRD_OPINION_OLLAMA_MODEL", "qwen2.5")
    monkeypatch.setenv("AI_THIRD_OPINION_OLLAMA_TIMEOUT_SECONDS", "75")
    calls = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        calls.append((advisor_input, api_key, model, timeout_seconds))
        return {
            "phase": "compression",
            "bias": "neutral",
            "market_view": "wait",
            "headline": "Wait: local compression",
            "pattern": "local compression",
            "confidence": 0.58,
            "timeframe": "5m",
            "evidence": ["local model sees compact range"],
            "contradictions": [],
            "invalidation": ["range expansion"],
            "data_quality": "confirmed",
            "_openai": {
                "provider": "ollama",
                "response_id": "2026-01-01T15:00:00Z",
                "model": "qwen2.5",
                "endpoint": "api/generate",
                "latency_ms": 1200.0,
                "usage": {"input_tokens": 33, "output_tokens": 11, "total_tokens": 44},
            },
        }

    monkeypatch.setattr(providers, "_request_ollama_verdict", fake_request)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["provider"] = "ollama"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert calls[0][1] == ""
    assert calls[0][2] == "qwen2.5-coder:14b"
    assert calls[0][3] == 75.0
    assert indicator["latest"]["source"] == "ollama"
    assert indicator["latest"]["connector"]["provider"] == "ollama"
    assert indicator["latest"]["connector"]["model"] == "qwen2.5"
    assert indicator["latest"]["connector"]["endpoint"] == "api/generate"
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "OK", "live"]


def test_ai_third_opinion_routes_codex_provider_without_openai_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_ADVISOR_BRIDGE_TOKEN", "bridge-token")
    calls = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        calls.append((advisor_input, api_key, model, timeout_seconds))
        return {
            "phase": "range",
            "bias": "neutral",
            "market_view": "wait",
            "headline": "Wait: range acceptance is unresolved",
            "pattern": "range acceptance",
            "confidence": 0.61,
            "timeframe": "5m",
            "evidence": ["confirmed bars remain inside the channel"],
            "contradictions": [],
            "invalidation": ["confirmed acceptance outside the channel"],
            "data_quality": "confirmed",
            "_openai": {
                "provider": "codex",
                "response_id": "codex_1",
                "model": "gpt-5.6-sol",
                "endpoint": "codex-exec",
                "latency_ms": 1250.0,
                "usage": {},
            },
        }

    monkeypatch.setattr(providers, "_request_codex_verdict", fake_request)
    snapshot = _snapshot()
    connector = snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]
    connector["provider"] = "codex"
    connector["model"] = "gpt-5.6-sol"

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    indicator = snapshot["indicators"]["ai_third_opinion"]
    assert calls[0][1] == "bridge-token"
    assert calls[0][2] == "gpt-5.6-sol"
    assert calls[0][3] == 40.0
    assert indicator["latest"]["source"] == "codex"
    assert indicator["latest"]["connector"]["provider"] == "codex"
    assert indicator["latest"]["connector"]["endpoint"] == "codex-exec"
    assert indicator["latest"]["signal"]["blocked_reason"] == "advisory_context_only"


def test_ai_third_opinion_consumes_typed_codex_bridge_errors() -> None:
    for bridge_status, connector_status in (
        ("auth_error", "auth_error"),
        ("rate_limited", "rate_limited"),
        ("timeout", "network_error"),
        ("model_missing", "model_missing"),
    ):
        error = providers._codex_provider_error(bridge_status, "bridge failure")
        assert advisor._connector_status_for_exception(error) == connector_status

    assert (
        advisor._connector_status_for_exception(
            RuntimeError("codex_rate_limited is display text, not a domain fact")
        )
        == "error"
    )


@pytest.mark.parametrize(
    ("http_status", "detail", "expected_status"),
    (
        (
            429,
            '{"error":{"code":"insufficient_quota","message":"quota exhausted"}}',
            "insufficient_quota",
        ),
        (429, "insufficient_quota", "rate_limited"),
        (429, "request rate exceeded", "rate_limited"),
        (401, "bad key", "auth_error"),
        (403, "denied", "forbidden"),
        (404, "model absent", "configuration_error"),
        (404, '{"error":{"code":"model_not_found","message":"absent"}}', "model_missing"),
    ),
)
def test_ai_provider_http_failures_have_typed_status(
    http_status: int,
    detail: str,
    expected_status: str,
) -> None:
    error = providers._http_provider_error("openai", http_status, detail)

    assert error.status == expected_status
    assert error.code == f"openai_http_{http_status}"


def test_ai_third_opinion_gate_opens_only_for_enabled_codex_provider(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setenv("CODEX_ADVISOR_BRIDGE_TOKEN", "bridge-token")

    def fake_request(path, payload, *, api_key, timeout_seconds):
        calls.append((path, payload))
        return {
            "ok": True,
            "status": "ready",
            "gate": "open" if path.endswith("/open") else "closed",
        }

    monkeypatch.setattr(providers, "_request_codex_payload", fake_request)

    opened = providers.set_ai_third_opinion_gate(True, "codex")
    closed = providers.set_ai_third_opinion_gate(True, "openai")

    assert opened["gate"] == "open"
    assert closed["gate"] == "closed"
    assert calls == [
        ("/gate/open", {"enabled": True, "provider": "codex"}),
        ("/gate/close", {"enabled": False, "provider": "openai"}),
    ]


def test_ai_third_opinion_gemini_quota_backoff_suppresses_repeat_calls(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("AI_THIRD_OPINION_RATE_LIMIT_BACKOFF_SECONDS", "30")
    calls = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        calls.append((advisor_input, api_key, model, timeout_seconds))
        raise providers.AiProviderError(
            provider="gemini",
            code="gemini_http_429",
            status="insufficient_quota",
            message="You do not have enough quota to make this request.",
            retryable=False,
        )

    monkeypatch.setattr(providers, "_request_gemini_verdict", fake_request)
    first = _snapshot()
    second = deepcopy(first)
    second["meta"]["analysis_ts"] = "2026-01-01T15:00:10+00:00"
    for snapshot in (first, second):
        snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"]["provider"] = "gemini"

    advisor.enrich_snapshot_with_ai_third_opinion(first)
    advisor.enrich_snapshot_with_ai_third_opinion(second)

    indicator = second["indicators"]["ai_third_opinion"]
    assert len(calls) == 1
    assert indicator["latest"]["connector"]["status"] == "insufficient_quota"
    assert indicator["latest"]["connector"]["provider"] == "gemini"
    assert "cooling down" in indicator["latest"]["connector"]["message"]
    assert indicator["table"]["columns"][4]["cells"] == ["HLTH", "NO $", "quota"]


def test_ai_third_opinion_can_disable_manual_channel_context(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    advisor._VERDICT_CACHE.clear()
    captured = []

    def fake_request(advisor_input, *, api_key, model, timeout_seconds):
        captured.append(advisor_input)
        return {
            "phase": "range",
            "bias": "neutral",
            "market_view": "wait",
            "headline": "Wait: range context",
            "pattern": "range context",
            "confidence": 0.6,
            "timeframe": "5m",
            "evidence": ["channel context disabled"],
            "contradictions": [],
            "invalidation": [],
            "data_quality": "confirmed",
            "_openai": {"usage": {"total_tokens": 10}},
        }

    monkeypatch.setattr(providers, "_request_openai_verdict", fake_request)
    snapshot = _snapshot()
    snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"][
        "channel_context_requested"
    ] = False

    advisor.enrich_snapshot_with_ai_third_opinion(snapshot)

    assert captured[0]["manual_channels"] == {"enabled": False}
    assert (
        snapshot["indicators"]["ai_third_opinion"]["latest"]["connector"][
            "channel_context_requested"
        ]
        is False
    )


def test_ai_third_opinion_uses_only_optional_channel_master_indicator_context() -> None:
    snapshot = _snapshot()
    legacy = deepcopy(snapshot["indicators"]["channel_master"])
    del snapshot["indicators"]["channel_master"]
    snapshot["manual_channels"] = legacy

    payload = advisor._advisor_input(
        snapshot,
        snapshot["indicators"]["ai_third_opinion"],
    )

    assert payload["manual_channels"] == {
        "enabled": True,
        "active": False,
        "count": 0,
        "channels": [],
        "nearest": None,
    }


def test_ai_third_opinion_discussion_uses_snapshot_context_and_history(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []

    def fake_discussion(*, advisor_input, question, history, api_key, model, timeout_seconds):
        captured.append((advisor_input, question, history, api_key, model, timeout_seconds))
        return {
            "answer": "Channel rejection is the main wait reason.",
            "object_suggestions": [],
            "response_id": "resp_discuss",
            "model": "gpt-test",
            "endpoint": "responses",
            "latency_ms": 33.0,
            "usage": {"input_tokens": 500, "output_tokens": 80, "total_tokens": 580},
        }

    monkeypatch.setattr(providers, "_request_openai_discussion", fake_discussion)

    payload = advisor.discuss_ai_third_opinion(
        _snapshot(),
        "why wait?",
        [{"role": "user", "content": "previous"}, {"role": "assistant", "content": "answer"}],
    )

    assert payload["ok"] is True
    assert payload["answer"] == "Channel rejection is the main wait reason."
    assert payload["usage"] == {"input_tokens": 500, "output_tokens": 80, "total_tokens": 580}
    assert payload["context"]["market_view"] == "long"
    assert captured[0][0]["manual_channels"]["nearest"]["role"] == "upper_edge"
    assert set(captured[0][0]) == {
        "meta",
        "bars",
        "candle_summary",
        "manual_channels",
        "live_bar",
        "higher_context",
    }
    assert captured[0][0]["candle_summary"]["count"] == 2
    assert "local_opinion" not in captured[0][0]
    assert "upstream" not in captured[0][0]
    assert "decision" not in captured[0][0]
    assert "direction_sentiment" not in captured[0][0]
    assert captured[0][1] == "why wait?"
    assert captured[0][2] == [
        {"role": "user", "content": "previous"},
        {"role": "assistant", "content": "answer"},
    ]


def test_ai_discussion_object_suggestions_use_standard_overlay_and_drawing_contracts(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_discussion(*, advisor_input, question, history, api_key, model, timeout_seconds):
        return {
            "answer": "I marked the confirmed range high and its target zone.",
            "object_suggestions": [
                {
                    "kind": "price_line",
                    "label": "Confirmed range high",
                    "rationale": "The level is the high of the latest confirmed bar.",
                    "direction": "neutral",
                    "top": None,
                    "bottom": None,
                    "price": 102.0,
                    "extend_bars": 24,
                    "confidence": 0.76,
                },
                {
                    "kind": "target_zone",
                    "label": "Target zone",
                    "rationale": "Confirmed bars bound this nearby acceptance area.",
                    "direction": "long",
                    "top": 103.0,
                    "bottom": 102.25,
                    "price": None,
                    "extend_bars": 18,
                    "confidence": 0.65,
                },
            ],
            "response_id": "resp_objects",
            "model": model,
            "endpoint": "responses",
            "usage": {},
        }

    monkeypatch.setattr(providers, "_request_openai_discussion", fake_discussion)

    payload = advisor.discuss_ai_third_opinion(
        _snapshot(),
        "Draw the range high and target zone.",
    )

    assert payload["ok"] is True
    assert len(payload["object_suggestions"]) == 2
    level, zone = payload["object_suggestions"]
    assert level["scope"] == {
        "instrument_id": "ibkr|future_root|ES",
        "route_fingerprint": "ibkr|future|ESM6",
        "timeframe": "5m",
    }
    assert level["overlay"]["type"] == "line"
    assert level["overlay"]["source"] == "ai_third_opinion"
    assert level["overlay"]["contract"] == "overlay-contract-v1"
    assert level["overlay"]["end_bar_offset"] == 24
    assert level["drawing"]["type"] == "line"
    assert level["drawing"]["extendRight"] is True
    assert zone["overlay"]["type"] == "box"
    assert zone["overlay"]["top"] == 103.0
    assert zone["overlay"]["bottom"] == 102.25
    assert zone["overlay"]["end_bar_offset"] == 18
    assert zone["drawing"]["type"] == "zone"
    assert zone["drawing"]["label"] == "Target zone"
    assert payload["context"]["latest_confirmed_bar_ts"] == "2026-01-01T15:00:00+00:00"


def test_ai_third_opinion_provider_test_reports_selected_model(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    captured = []

    def fake_discussion(*, advisor_input, question, history, api_key, model, timeout_seconds):
        captured.append((advisor_input, question, history, api_key, model, timeout_seconds))
        return {
            "answer": "OK deepseek reachable",
            "object_suggestions": [],
            "provider": "deepseek",
            "model": model,
            "endpoint": "chat/completions",
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }

    monkeypatch.setattr(providers, "_request_deepseek_discussion", fake_discussion)

    payload = advisor.test_ai_third_opinion_provider("deepseek", "deepseek-v4-flash")

    assert payload["ok"] is True
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["endpoint"] == "chat/completions"
    assert payload["health"] == {"state": "connected", "label": "OK", "detail": "live"}
    assert payload["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    assert captured[0][0]["candle_summary"]["count"] == 0
    assert captured[0][1].startswith("Connection test.")
    assert captured[0][3] == "deepseek-key"


def test_ai_third_opinion_provider_test_classifies_quota(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    def fake_discussion(*args, **kwargs):
        raise providers.AiProviderError(
            provider="deepseek",
            code="deepseek_http_429",
            status="insufficient_quota",
            message="not enough quota",
            retryable=False,
        )

    monkeypatch.setattr(providers, "_request_deepseek_discussion", fake_discussion)

    payload = advisor.test_ai_third_opinion_provider("deepseek", "deepseek-v4-flash")

    assert payload["ok"] is False
    assert payload["status"] == "insufficient_quota"
    assert payload["health"] == {"state": "insufficient_quota", "label": "NO $", "detail": "quota"}
    assert "deepseek_http_429" in payload["message"]


def test_ai_third_opinion_discussion_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    payload = advisor.discuss_ai_third_opinion(_snapshot(), "why?")

    assert payload["ok"] is False
    assert payload["status"] == "missing_api_key"


def test_ai_third_opinion_reads_openai_key_from_secret_file(monkeypatch, tmp_path) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=secret-file-key",
                "DEEPSEEK_API_KEY=deepseek-secret-file-key",
                "GEMINI_API_KEY=gemini-secret-file-key",
                "AI_THIRD_OPINION_MODEL=gpt-secret",
                "AI_THIRD_OPINION_DEEPSEEK_MODEL=deepseek-secret",
                "AI_THIRD_OPINION_GEMINI_MODEL=gemini-secret",
                "AI_THIRD_OPINION_OLLAMA_MODEL=ollama-secret",
                "AI_THIRD_OPINION_CODEX_MODEL=gpt-5.6-sol",
                "CODEX_ADVISOR_BRIDGE_TOKEN=codex-secret-file-token",
                "AI_THIRD_OPINION_CODEX_TIMEOUT_SECONDS=39",
                "AI_THIRD_OPINION_TIMEOUT_SECONDS=7",
                "AI_THIRD_OPINION_OLLAMA_TIMEOUT_SECONDS=66",
                "OPENAI_BASE_URL=https://example.test/v1",
                "DEEPSEEK_BASE_URL=https://deepseek.example.test",
                "GEMINI_BASE_URL=https://gemini.example.test/v1beta",
                "OLLAMA_BASE_URL=http://ollama.example.test:11434",
                "CODEX_ADVISOR_BASE_URL=http://codex.example.test:8765",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_CODEX_MODEL", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_OLLAMA_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AI_THIRD_OPINION_CODEX_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_ADVISOR_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_ADVISOR_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("AEF_SECRET_FILE", str(secret_file))
    monkeypatch.setattr(app_config, "_SECRET_FILE_CACHE", None)

    assert providers.api_key() == "secret-file-key"
    assert providers.api_key("deepseek") == "deepseek-secret-file-key"
    assert providers.api_key("gemini") == "gemini-secret-file-key"
    assert providers.api_key("ollama") == ""
    assert providers.api_key("codex") == "codex-secret-file-token"
    assert providers.model_name() == "gpt-secret"
    assert providers.model_name("deepseek") == "deepseek-secret"
    assert providers.model_name("gemini") == "gemini-secret"
    assert providers.model_name("ollama") == "ollama-secret"
    assert providers.model_name("codex") == "gpt-5.6-sol"
    assert providers.timeout_seconds() == 7.0
    assert providers.timeout_seconds("ollama") == 66.0
    assert providers.timeout_seconds("codex") == 39.0
    assert providers.base_url() == "https://example.test/v1"
    assert providers.base_url("deepseek") == "https://deepseek.example.test"
    assert providers.base_url("gemini") == "https://gemini.example.test/v1beta"
    assert providers.base_url("ollama") == "http://ollama.example.test:11434"
    assert providers.base_url("codex") == "http://codex.example.test:8765"
