from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aef_terminal.config import env_or_secret

AI_THIRD_OPINION_MODEL_ENV = "AI_THIRD_OPINION_MODEL"

AI_THIRD_OPINION_PROVIDER_ENV = "AI_THIRD_OPINION_PROVIDER"

AI_THIRD_OPINION_OPENAI_MODEL_ENV = "AI_THIRD_OPINION_OPENAI_MODEL"

AI_THIRD_OPINION_DEEPSEEK_MODEL_ENV = "AI_THIRD_OPINION_DEEPSEEK_MODEL"

AI_THIRD_OPINION_GEMINI_MODEL_ENV = "AI_THIRD_OPINION_GEMINI_MODEL"

AI_THIRD_OPINION_OLLAMA_MODEL_ENV = "AI_THIRD_OPINION_OLLAMA_MODEL"

AI_THIRD_OPINION_CODEX_MODEL_ENV = "AI_THIRD_OPINION_CODEX_MODEL"

AI_THIRD_OPINION_TIMEOUT_ENV = "AI_THIRD_OPINION_TIMEOUT_SECONDS"

AI_THIRD_OPINION_OLLAMA_TIMEOUT_ENV = "AI_THIRD_OPINION_OLLAMA_TIMEOUT_SECONDS"

AI_THIRD_OPINION_CODEX_TIMEOUT_ENV = "AI_THIRD_OPINION_CODEX_TIMEOUT_SECONDS"

AI_THIRD_OPINION_BACKOFF_ENV = "AI_THIRD_OPINION_RATE_LIMIT_BACKOFF_SECONDS"

AI_THIRD_OPINION_CONTEXT_BARS_ENV = "AI_THIRD_OPINION_CONTEXT_BARS"

AI_THIRD_OPINION_BASE_URL_ENV = "OPENAI_BASE_URL"

AI_THIRD_OPINION_DEEPSEEK_BASE_URL_ENV = "DEEPSEEK_BASE_URL"

AI_THIRD_OPINION_GEMINI_BASE_URL_ENV = "GEMINI_BASE_URL"

AI_THIRD_OPINION_OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"

AI_THIRD_OPINION_CODEX_BASE_URL_ENV = "CODEX_ADVISOR_BASE_URL"

AI_THIRD_OPINION_CODEX_TOKEN_ENV = "CODEX_ADVISOR_BRIDGE_TOKEN"

DEFAULT_PROVIDER = "openai"

DEFAULT_OPENAI_MODEL = "gpt-5.5"

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

DEFAULT_OLLAMA_MODEL = "llama3.2"

DEFAULT_CODEX_MODEL = "gpt-5.6-terra"

DEFAULT_TIMEOUT_SECONDS = 12.0

DEFAULT_OLLAMA_TIMEOUT_SECONDS = 60.0

DEFAULT_CODEX_TIMEOUT_SECONDS = 40.0

DEFAULT_BACKOFF_SECONDS = 300.0

DEFAULT_EXTERNAL_CONTEXT_BARS = 100

MAX_EXTERNAL_CONTEXT_BARS = 160

DEFAULT_OLLAMA_CONTEXT_BARS = 100

OLLAMA_MODEL_ALIASES = {
    "deepseek-r1": "deepseek-r1:8b",
    "qwen2.5": "qwen2.5-coder:14b",
}

_DISCUSSION_MAX_HISTORY = 8

_PROVIDER_BACKOFF: dict[str, dict[str, Any]] = {}

AI_PROVIDER_ERROR_STATUSES = frozenset(
    {
        "error",
        "insufficient_quota",
        "rate_limited",
        "auth_error",
        "forbidden",
        "configuration_error",
        "network_error",
        "model_missing",
    }
)


class AiProviderError(RuntimeError):
    """Typed transport/provider failure consumed without parsing display text."""

    def __init__(
        self,
        *,
        provider: str,
        code: str,
        status: str,
        message: str = "",
        retryable: bool = False,
    ) -> None:
        if status not in AI_PROVIDER_ERROR_STATUSES:
            raise ValueError("AI_PROVIDER_ERROR_STATUS_INVALID")
        self.provider = normalize_provider(provider)
        self.code = str(code or "AI_PROVIDER_ERROR").strip()
        self.status = status
        self.retryable = bool(retryable)
        self.detail = str(message or "").strip()
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


def _typed_provider_error_codes(detail: str) -> frozenset[str]:
    try:
        payload = json.loads(detail)
    except TypeError, json.JSONDecodeError:
        return frozenset()
    candidates: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"code", "type", "status", "reason"} and isinstance(item, str):
                    candidates.add(item.strip().casefold())
                elif key in {"error", "errors", "details"}:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    return frozenset(candidates)


def _http_provider_error(provider: str, status_code: int, detail: str) -> AiProviderError:
    typed_codes = _typed_provider_error_codes(detail)
    if status_code == 402 or typed_codes.intersection(
        {"insufficient_quota", "billing_hard_limit_reached"}
    ):
        status = "insufficient_quota"
    elif status_code == 429:
        status = "rate_limited"
    elif status_code == 401:
        status = "auth_error"
    elif status_code == 403:
        status = "forbidden"
    elif typed_codes.intersection({"model_not_found", "model_missing"}):
        status = "model_missing"
    elif status_code == 404:
        status = "configuration_error"
    else:
        status = "error"
    return AiProviderError(
        provider=provider,
        code=f"{provider}_http_{status_code}",
        status=status,
        message=detail,
        retryable=status in {"rate_limited", "network_error", "error"},
    )


def _network_provider_error(provider: str, error: BaseException) -> AiProviderError:
    return AiProviderError(
        provider=provider,
        code=f"{provider}_network_error",
        status="network_error",
        message=str(error),
        retryable=True,
    )


def _codex_provider_error(status: object, message: object) -> AiProviderError:
    exact_status = str(status or "bridge_error").strip()
    if exact_status.startswith("http_") and exact_status[5:].isdigit():
        return _http_provider_error("codex", int(exact_status[5:]), str(message or ""))
    connector_status = {
        "insufficient_quota": "insufficient_quota",
        "auth_error": "auth_error",
        "rate_limited": "rate_limited",
        "timeout": "network_error",
        "network_error": "network_error",
        "model_missing": "model_missing",
        "forbidden": "forbidden",
        "configuration_error": "configuration_error",
    }.get(exact_status, "error")
    return AiProviderError(
        provider="codex",
        code=f"codex_{exact_status}",
        status=connector_status,
        message=str(message or "Codex bridge request failed.").strip(),
        retryable=connector_status in {"rate_limited", "network_error", "error"},
    )


AI_THIRD_OPINION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["trend_up", "trend_down", "range", "compression", "reversal_risk", "unclear"],
        },
        "bias": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral"],
        },
        "market_view": {
            "type": "string",
            "enum": ["long", "short", "wait"],
        },
        "headline": {"type": "string"},
        "pattern": {"type": "string"},
        "confidence": {
            "type": "number",
        },
        "timeframe": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "contradictions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "invalidation": {
            "type": "array",
            "items": {"type": "string"},
        },
        "data_quality": {
            "type": "string",
            "enum": ["confirmed", "stale", "incomplete"],
        },
    },
    "required": [
        "phase",
        "bias",
        "market_view",
        "headline",
        "pattern",
        "confidence",
        "timeframe",
        "evidence",
        "contradictions",
        "invalidation",
        "data_quality",
    ],
}

AI_THIRD_OPINION_DISCUSSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string"},
        "object_suggestions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["target_zone", "price_line"],
                    },
                    "label": {"type": "string"},
                    "rationale": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["long", "short", "neutral"],
                    },
                    "top": {"type": ["number", "null"]},
                    "bottom": {"type": ["number", "null"]},
                    "price": {"type": ["number", "null"]},
                    "extend_bars": {
                        "type": "integer",
                        "minimum": 4,
                        "maximum": 80,
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": [
                    "kind",
                    "label",
                    "rationale",
                    "direction",
                    "top",
                    "bottom",
                    "price",
                    "extend_bars",
                    "confidence",
                ],
            },
        },
    },
    "required": ["answer", "object_suggestions"],
}


def _latest_indicator_state(indicator: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(indicator, dict):
        return {}
    latest = indicator.get("latest")
    return latest if isinstance(latest, dict) else {}


def context_bar_limit(provider: str = DEFAULT_PROVIDER) -> int:
    provider_name = str(provider or DEFAULT_PROVIDER).strip().lower()
    default = (
        DEFAULT_OLLAMA_CONTEXT_BARS if provider_name == "ollama" else DEFAULT_EXTERNAL_CONTEXT_BARS
    )
    high = MAX_EXTERNAL_CONTEXT_BARS
    try:
        value = int(
            float(env_or_secret(AI_THIRD_OPINION_CONTEXT_BARS_ENV, str(default)) or default)
        )
    except TypeError, ValueError:
        value = default
    return max(8, min(value, high))


def timeout_seconds(provider: str | None = None) -> float:
    if provider == "ollama":
        try:
            return max(
                5.0,
                min(
                    float(
                        env_or_secret(
                            AI_THIRD_OPINION_OLLAMA_TIMEOUT_ENV, str(DEFAULT_OLLAMA_TIMEOUT_SECONDS)
                        )
                        or DEFAULT_OLLAMA_TIMEOUT_SECONDS
                    ),
                    240.0,
                ),
            )
        except TypeError, ValueError:
            return DEFAULT_OLLAMA_TIMEOUT_SECONDS
    if provider == "codex":
        try:
            return max(
                5.0,
                min(
                    float(
                        env_or_secret(
                            AI_THIRD_OPINION_CODEX_TIMEOUT_ENV, str(DEFAULT_CODEX_TIMEOUT_SECONDS)
                        )
                        or DEFAULT_CODEX_TIMEOUT_SECONDS
                    ),
                    42.0,
                ),
            )
        except TypeError, ValueError:
            return DEFAULT_CODEX_TIMEOUT_SECONDS
    try:
        return max(
            1.0,
            min(
                float(
                    env_or_secret(AI_THIRD_OPINION_TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECONDS))
                    or DEFAULT_TIMEOUT_SECONDS
                ),
                45.0,
            ),
        )
    except TypeError, ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _backoff_seconds() -> float:
    try:
        return max(
            30.0,
            min(
                float(
                    env_or_secret(AI_THIRD_OPINION_BACKOFF_ENV, str(DEFAULT_BACKOFF_SECONDS))
                    or DEFAULT_BACKOFF_SECONDS
                ),
                3600.0,
            ),
        )
    except TypeError, ValueError:
        return DEFAULT_BACKOFF_SECONDS


def provider_backoff_remaining(provider: str) -> tuple[str, int, str] | None:
    payload = _PROVIDER_BACKOFF.get(provider)
    if not isinstance(payload, dict):
        return None
    until = float(payload.get("until") or 0.0)
    remaining = int(round(until - time.monotonic()))
    if remaining <= 0:
        _PROVIDER_BACKOFF.pop(provider, None)
        return None
    status = str(payload.get("status") or "rate_limited")
    message = str(payload.get("message") or "").strip()
    return status, remaining, message


def provider_backoff_message(
    provider: str, status: str, remaining_seconds: int, detail: str = ""
) -> str:
    label = status.replace("_", " ")
    message = f"{provider.upper()} connector is cooling down after {label} response; retry in {remaining_seconds}s."
    if detail:
        message += f" Last API error: {detail[:180]}"
    return message


def note_provider_backoff(provider: str, status: str, message: str) -> None:
    _PROVIDER_BACKOFF[provider] = {
        "until": time.monotonic() + _backoff_seconds(),
        "status": status,
        "message": message[:240],
    }


def clear_provider_backoff(provider: str) -> None:
    _PROVIDER_BACKOFF.pop(provider, None)


def provider_name(indicator: dict[str, Any] | None = None) -> str:
    latest = _latest_indicator_state(indicator)
    connector = latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    raw = (
        connector.get("provider")
        or env_or_secret(AI_THIRD_OPINION_PROVIDER_ENV)
        or DEFAULT_PROVIDER
    )
    return normalize_provider(raw)


def normalize_provider(raw: Any) -> str:
    provider = str(raw or "").strip().lower()
    return (
        provider
        if provider in {"openai", "deepseek", "gemini", "ollama", "codex"}
        else DEFAULT_PROVIDER
    )


def model_name(provider: str | None = None, indicator: dict[str, Any] | None = None) -> str:
    provider_key = provider or provider_name(indicator)
    latest = _latest_indicator_state(indicator)
    connector = latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    selected = str(connector.get("model") or "").strip()
    provider_prefixes = {
        "openai": ("gpt-", "o"),
        "deepseek": ("deepseek-",),
        "gemini": ("gemini-",),
        "codex": ("gpt-5.6-",),
    }
    if provider_key == "ollama":
        cloud_presets = {
            "gpt-5.5",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        }
        if selected and selected != "default" and selected not in cloud_presets:
            return OLLAMA_MODEL_ALIASES.get(selected, selected)
        model = (
            env_or_secret(AI_THIRD_OPINION_OLLAMA_MODEL_ENV)
            or env_or_secret("OLLAMA_MODEL")
            or DEFAULT_OLLAMA_MODEL
        )
        return OLLAMA_MODEL_ALIASES.get(str(model), str(model))
    if (
        selected
        and selected != "default"
        and selected.startswith(provider_prefixes.get(provider_key, ()))
    ):
        return selected
    if provider_key == "codex":
        return (
            env_or_secret(AI_THIRD_OPINION_CODEX_MODEL_ENV)
            or env_or_secret("CODEX_ADVISOR_MODEL")
            or DEFAULT_CODEX_MODEL
        )
    if provider_key == "deepseek":
        return (
            env_or_secret(AI_THIRD_OPINION_DEEPSEEK_MODEL_ENV)
            or env_or_secret("DEEPSEEK_MODEL")
            or DEFAULT_DEEPSEEK_MODEL
        )
    if provider_key == "gemini":
        return (
            env_or_secret(AI_THIRD_OPINION_GEMINI_MODEL_ENV)
            or env_or_secret("GEMINI_MODEL")
            or DEFAULT_GEMINI_MODEL
        )
    return (
        env_or_secret(AI_THIRD_OPINION_OPENAI_MODEL_ENV)
        or env_or_secret(AI_THIRD_OPINION_MODEL_ENV)
        or env_or_secret("OPENAI_MODEL")
        or DEFAULT_OPENAI_MODEL
    )


def base_url(provider: str = DEFAULT_PROVIDER) -> str:
    if provider == "deepseek":
        return (
            env_or_secret(AI_THIRD_OPINION_DEEPSEEK_BASE_URL_ENV) or "https://api.deepseek.com"
        ).rstrip("/")
    if provider == "gemini":
        return (
            env_or_secret(AI_THIRD_OPINION_GEMINI_BASE_URL_ENV)
            or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
    if provider == "ollama":
        return (
            env_or_secret(AI_THIRD_OPINION_OLLAMA_BASE_URL_ENV) or "http://127.0.0.1:11434"
        ).rstrip("/")
    if provider == "codex":
        return (
            env_or_secret(AI_THIRD_OPINION_CODEX_BASE_URL_ENV) or "http://host.docker.internal:8765"
        ).rstrip("/")
    return (env_or_secret(AI_THIRD_OPINION_BASE_URL_ENV) or "https://api.openai.com/v1").rstrip("/")


def api_key(provider: str = DEFAULT_PROVIDER) -> str:
    if provider == "deepseek":
        return str(env_or_secret("DEEPSEEK_API_KEY") or "").strip()
    if provider == "gemini":
        return str(env_or_secret("GEMINI_API_KEY") or env_or_secret("GOOGLE_API_KEY") or "").strip()
    if provider == "ollama":
        return ""
    if provider == "codex":
        return str(env_or_secret(AI_THIRD_OPINION_CODEX_TOKEN_ENV) or "").strip()
    return str(env_or_secret("OPENAI_API_KEY") or "").strip()


def api_key_name(provider: str = DEFAULT_PROVIDER) -> str:
    if provider == "deepseek":
        return "DEEPSEEK_API_KEY"
    if provider == "gemini":
        return "GEMINI_API_KEY"
    if provider == "ollama":
        return ""
    if provider == "codex":
        return AI_THIRD_OPINION_CODEX_TOKEN_ENV
    return "OPENAI_API_KEY"


def _verdict_system_prompt() -> str:
    return (
        "You are an advisory market-context reviewer for a private trading terminal. "
        "Use only the supplied confirmed bars, candle_summary derived from those bars, and user-drawn manual channel context. "
        "Do not use other indicator outputs or trading engine decisions. "
        "Do not produce execution commands, orders, or authoritative candles. "
        "Return a cautious third opinion about current market phase. "
        "market_view must be your direct/raw AI view of where to pay attention next: long, short, or wait. "
        "Do not invert or apply a contrarian interpretation; the terminal may do that separately. "
        "It is not a trade command. candle_summary is a compact OHLCV summary, not a separate indicator. User-drawn manual_channels are chart context when included: "
        "use nearest channel edge, midline, touch, reclaim, inside/outside, slope, and target context "
        "to identify false breakouts, channel rejection, channel reclaim, or early trend formation. "
        "Compare numeric prices explicitly before saying above, below, support, resistance, rejection, or reclaim; "
        "do not infer direction from a channel label such as high, low, upper, lower, ghost_high, or ghost_low. "
        "For manual channel levels, relation_to_price and distance_points are deterministic fields from the terminal; prefer them over your own arithmetic. "
        "primary_path, no_fade, break_score, bounce_score, and raw_break_score are also deterministic backend facts; "
        "when no_fade is true, do not propose a channel fade unless a later confirmed reclaim is supplied. "
        "If a close is numerically below a channel level, state below; if it is above, state above; if the supplied channel context conflicts, say uncertain. "
        "headline and pattern should be short tooltip-ready phrases, "
        "for example false breakout, early trend formation, compression before expansion, or range rejection."
    )


def _discussion_system_prompt() -> str:
    return (
        "You are the discussion mode for AI Third Opinion inside a private trading terminal. "
        "Answer the user's market-context questions using only the supplied confirmed bars, candle_summary, "
        "multi-window higher_context derived only from those confirmed base-timeframe bars, "
        "the separately typed provisional live_bar, user-drawn channel context, and this discussion history. "
        "higher_context is broader-horizon context, not a set of provider-native higher-timeframe candles; do not claim otherwise. "
        "Never describe live_bar as closed, confirmed, or authoritative; use it only as provisional intrabar context "
        "and explicitly distinguish it from confirmed bars when it affects the answer. "
        "Do not use other indicator outputs or trading engine decisions. "
        "Do not recommend order execution, position sizing, or certainty. "
        "Compare numeric prices explicitly before saying above, below, support, resistance, rejection, or reclaim; "
        "do not infer direction from a channel label such as high, low, upper, lower, ghost_high, or ghost_low. "
        "For manual channel levels, relation_to_price and distance_points are deterministic fields from the terminal; prefer them over your own arithmetic. "
        "primary_path, no_fade, break_score, bounce_score, and raw_break_score are also deterministic backend facts; "
        "when no_fade is true, do not propose a channel fade unless a later confirmed reclaim is supplied. "
        "If a close is numerically below a channel level, state below; if it is above, state above; if the supplied channel context conflicts, say uncertain. "
        "Explain phase, long/short/wait context, invalidation, channel rejection/reclaim, "
        "false breakout risk, and what would change the view. Keep answers concise and practical. "
        "Return object_suggestions only when the user explicitly asks to draw, mark, show, or add a target zone or price level on the chart/canvas. "
        "For target_zone set top and bottom and set price to null; for price_line set price and set top and bottom to null. "
        "Use only numeric levels supported by the supplied context. If geometry is not justified, return an empty object_suggestions array. "
        "Suggestions are advisory previews requiring explicit user confirmation; they are never execution commands."
    )


def _request_body(model: str, advisor_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": _verdict_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(advisor_input, separators=(",", ":"), default=str),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ai_third_opinion",
                "strict": True,
                "schema": AI_THIRD_OPINION_SCHEMA,
            }
        },
        "max_output_tokens": 900,
    }


def _chat_verdict_body(model: str, advisor_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    _verdict_system_prompt()
                    + " Return only valid json matching this schema: "
                    + json.dumps(AI_THIRD_OPINION_SCHEMA, separators=(",", ":"), default=str)
                ),
            },
            {
                "role": "user",
                "content": json.dumps(advisor_input, separators=(",", ":"), default=str),
            },
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": 900,
    }


def _gemini_verdict_body(model: str, advisor_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": model,
        "input": (
            _verdict_system_prompt()
            + "\nReturn only valid JSON matching the supplied schema."
            + "\nMarket context JSON:\n"
            + json.dumps(advisor_input, separators=(",", ":"), default=str)
        ),
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": AI_THIRD_OPINION_SCHEMA,
        },
    }


def _ollama_verdict_body(model: str, advisor_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": (
            _verdict_system_prompt()
            + "\nRespond using JSON only and match the supplied schema."
            + "\nMarket context JSON:\n"
            + json.dumps(advisor_input, separators=(",", ":"), default=str)
        ),
        "stream": False,
        "format": AI_THIRD_OPINION_SCHEMA,
        "options": {"temperature": 0.2},
        "keep_alive": "10m",
    }


def _gemini_discussion_body(
    model: str, advisor_input: dict[str, Any], question: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "model": model,
        "input": (
            _discussion_system_prompt()
            + "\nReturn only valid JSON matching the supplied schema. Use this JSON payload only:\n"
            + json.dumps(
                {
                    "question": question[:2000],
                    "conversation_history": history,
                    "market_context": advisor_input,
                },
                separators=(",", ":"),
                default=str,
            )
        ),
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": AI_THIRD_OPINION_DISCUSSION_SCHEMA,
        },
    }


def _ollama_discussion_body(
    model: str, advisor_input: dict[str, Any], question: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": (
            _discussion_system_prompt()
            + "\nUse this JSON payload only and return JSON matching the supplied schema:\n"
            + json.dumps(
                {
                    "question": question[:2000],
                    "conversation_history": history,
                    "market_context": advisor_input,
                },
                separators=(",", ":"),
                default=str,
            )
        ),
        "stream": False,
        "format": AI_THIRD_OPINION_DISCUSSION_SCHEMA,
        "options": {"temperature": 0.2},
        "keep_alive": "10m",
    }


def _extract_response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return ""


def _extract_chat_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, list):
                continue
            texts = [
                part.get("text")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if texts:
                return "".join(texts)
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            texts = [
                part.get("text")
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if texts:
                return "".join(texts)
    return _extract_response_text(payload)


def _token_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = payload.get("usageMetadata") or payload.get("usage_metadata")
    if not isinstance(usage, dict):
        return {}

    def _int_field(name: str) -> int | None:
        try:
            value = int(usage.get(name))
        except TypeError, ValueError:
            return None
        return value if value >= 0 else None

    out: dict[str, Any] = {}
    for target, sources in (
        (
            "input_tokens",
            (
                "input_tokens",
                "total_input_tokens",
                "prompt_tokens",
                "promptTokenCount",
                "prompt_eval_count",
            ),
        ),
        (
            "output_tokens",
            (
                "output_tokens",
                "total_output_tokens",
                "completion_tokens",
                "candidatesTokenCount",
                "eval_count",
            ),
        ),
        ("total_tokens", ("total_tokens", "totalTokenCount")),
    ):
        for source in sources:
            value = _int_field(source)
            if value is not None:
                out[target] = value
                break
    if (
        "total_tokens" not in out
        and isinstance(out.get("input_tokens"), int)
        and isinstance(out.get("output_tokens"), int)
    ):
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    for detail_key in ("input_tokens_details", "output_tokens_details"):
        details = usage.get(detail_key)
        if isinstance(details, dict):
            out[detail_key] = {
                str(key): value
                for key, value in details.items()
                if isinstance(value, (int, float, str, bool)) or value is None
            }
    return out


def _request_openai_verdict(
    advisor_input: dict[str, Any], *, api_key: str, model: str, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    body = json.dumps(
        _request_body(model, advisor_input), separators=(",", ":"), default=str
    ).encode("utf-8")
    request = Request(
        f"{base_url('openai')}/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("openai", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("openai", exc) from exc
    text = _extract_response_text(response_payload)
    if not text:
        raise RuntimeError("openai_empty_output")
    verdict = json.loads(text)
    if not isinstance(verdict, dict):
        raise RuntimeError("openai_invalid_json_output")
    verdict["_openai"] = {
        "provider": "openai",
        "response_id": response_payload.get("id"),
        "model": response_payload.get("model") or model,
        "endpoint": "responses",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }
    return verdict


def _request_deepseek_verdict(
    advisor_input: dict[str, Any], *, api_key: str, model: str, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    body = json.dumps(
        _chat_verdict_body(model, advisor_input), separators=(",", ":"), default=str
    ).encode("utf-8")
    request = Request(
        f"{base_url('deepseek')}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("deepseek", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("deepseek", exc) from exc
    text = _extract_chat_text(response_payload).strip()
    if not text:
        raise RuntimeError("deepseek_empty_output")
    verdict = json.loads(text)
    if not isinstance(verdict, dict):
        raise RuntimeError("deepseek_invalid_json_output")
    verdict["_openai"] = {
        "provider": "deepseek",
        "response_id": response_payload.get("id"),
        "model": response_payload.get("model") or model,
        "endpoint": "chat/completions",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }
    return verdict


def _request_gemini_verdict(
    advisor_input: dict[str, Any], *, api_key: str, model: str, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    request = Request(
        f"{base_url('gemini')}/interactions",
        data=json.dumps(
            _gemini_verdict_body(model, advisor_input), separators=(",", ":"), default=str
        ).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("gemini", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("gemini", exc) from exc
    text = _extract_gemini_text(response_payload).strip()
    if not text:
        raise RuntimeError("gemini_empty_output")
    verdict = json.loads(text)
    if not isinstance(verdict, dict):
        raise RuntimeError("gemini_invalid_json_output")
    verdict["_openai"] = {
        "provider": "gemini",
        "response_id": response_payload.get("id") or response_payload.get("name"),
        "model": response_payload.get("model") or model,
        "endpoint": "interactions",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }
    return verdict


def _request_ollama_verdict(
    advisor_input: dict[str, Any], *, api_key: str, model: str, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    request = Request(
        f"{base_url('ollama')}/api/generate",
        data=json.dumps(
            _ollama_verdict_body(model, advisor_input), separators=(",", ":"), default=str
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("ollama", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("ollama", exc) from exc
    text = str(response_payload.get("response") or "").strip()
    if not text:
        raise RuntimeError("ollama_empty_output")
    verdict = json.loads(text)
    if not isinstance(verdict, dict):
        raise RuntimeError("ollama_invalid_json_output")
    verdict["_openai"] = {
        "provider": "ollama",
        "response_id": response_payload.get("created_at"),
        "model": response_payload.get("model") or model,
        "endpoint": "api/generate",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage({"usage": response_payload}),
    }
    return verdict


def _request_codex_payload(
    path: str,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    request = Request(
        f"{base_url('codex')}{path}",
        data=json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        try:
            error_payload = json.loads(detail)
        except json.JSONDecodeError:
            error_payload = {}
        status = str(error_payload.get("status") or f"http_{exc.code}").strip()
        message = str(error_payload.get("message") or detail or exc.reason).strip()
        raise _codex_provider_error(status, message) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("codex", exc) from exc
    if not isinstance(response_payload, dict):
        raise AiProviderError(
            provider="codex",
            code="codex_invalid_response",
            status="error",
        )
    if response_payload.get("ok") is not True:
        status = str(response_payload.get("status") or "bridge_error").strip()
        message = str(response_payload.get("message") or "Codex bridge request failed.").strip()
        raise _codex_provider_error(status, message)
    return response_payload


def set_ai_third_opinion_gate(enabled: Any, provider: Any = DEFAULT_PROVIDER) -> dict[str, Any]:
    provider_name = normalize_provider(provider)
    gate_open = bool(enabled) and provider_name == "codex"
    bridge_token = api_key("codex")
    if not bridge_token:
        return {
            "ok": False,
            "status": "missing_api_key",
            "provider": "codex",
            "gate": "closed",
            "message": f"{AI_THIRD_OPINION_CODEX_TOKEN_ENV} is not configured.",
        }
    try:
        response = _request_codex_payload(
            "/gate/open" if gate_open else "/gate/close",
            {"enabled": gate_open, "provider": provider_name},
            api_key=bridge_token,
            timeout_seconds=min(timeout_seconds("codex"), 5.0),
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return {
            "ok": False,
            "status": "bridge_unavailable",
            "provider": "codex",
            "gate": "unknown",
            "message": message[:1000],
        }
    return {
        "ok": True,
        "status": str(response.get("status") or "ready"),
        "provider": "codex",
        "gate": str(response.get("gate") or ("open" if gate_open else "closed")),
    }


def _request_codex_verdict(
    advisor_input: dict[str, Any], *, api_key: str, model: str, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    response_payload = _request_codex_payload(
        "/verdict",
        {
            "model": model,
            "advisor_input": advisor_input,
        },
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    verdict = response_payload.get("verdict")
    if not isinstance(verdict, dict):
        raise RuntimeError("codex_invalid_json_output")
    verdict["_openai"] = {
        "provider": "codex",
        "response_id": response_payload.get("response_id"),
        "model": response_payload.get("model") or model,
        "endpoint": response_payload.get("endpoint") or "codex-exec",
        "latency_ms": response_payload.get("latency_ms")
        or round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }
    return verdict


def compact_discussion_history(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    out: list[dict[str, str]] = []
    for item in history[-_DISCUSSION_MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content[:1200]})
    return out


def _parse_discussion_output(text: Any, *, provider: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise RuntimeError(f"{provider}_empty_output")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{provider}_invalid_json_output") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider}_invalid_json_output")
    answer = str(payload.get("answer") or "").strip()
    suggestions = payload.get("object_suggestions")
    if not answer or not isinstance(suggestions, list):
        raise RuntimeError(f"{provider}_invalid_discussion_contract")
    return {
        "answer": answer,
        "object_suggestions": [dict(item) for item in suggestions[:3] if isinstance(item, Mapping)],
    }


def _request_openai_discussion(
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": _discussion_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question[:2000],
                        "conversation_history": history,
                        "market_context": advisor_input,
                    },
                    separators=(",", ":"),
                    default=str,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ai_third_opinion_discussion",
                "strict": True,
                "schema": AI_THIRD_OPINION_DISCUSSION_SCHEMA,
            }
        },
        "max_output_tokens": 1100,
    }
    request = Request(
        f"{base_url('openai')}/responses",
        data=json.dumps(body, separators=(",", ":"), default=str).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("openai", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("openai", exc) from exc
    discussion = _parse_discussion_output(
        _extract_response_text(response_payload),
        provider="openai",
    )
    return {
        **discussion,
        "provider": "openai",
        "response_id": response_payload.get("id"),
        "model": response_payload.get("model") or model,
        "endpoint": "responses",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }


def _request_deepseek_discussion(
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    _discussion_system_prompt()
                    + " Return only valid JSON matching this schema: "
                    + json.dumps(
                        AI_THIRD_OPINION_DISCUSSION_SCHEMA,
                        separators=(",", ":"),
                    )
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question[:2000],
                        "conversation_history": history,
                        "market_context": advisor_input,
                    },
                    separators=(",", ":"),
                    default=str,
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": 1100,
    }
    request = Request(
        f"{base_url('deepseek')}/chat/completions",
        data=json.dumps(body, separators=(",", ":"), default=str).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("deepseek", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("deepseek", exc) from exc
    discussion = _parse_discussion_output(
        _extract_chat_text(response_payload),
        provider="deepseek",
    )
    return {
        **discussion,
        "provider": "deepseek",
        "response_id": response_payload.get("id"),
        "model": response_payload.get("model") or model,
        "endpoint": "chat/completions",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }


def _request_gemini_discussion(
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    request = Request(
        f"{base_url('gemini')}/interactions",
        data=json.dumps(
            _gemini_discussion_body(model, advisor_input, question, history),
            separators=(",", ":"),
            default=str,
        ).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("gemini", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("gemini", exc) from exc
    discussion = _parse_discussion_output(
        _extract_gemini_text(response_payload),
        provider="gemini",
    )
    return {
        **discussion,
        "provider": "gemini",
        "response_id": response_payload.get("id") or response_payload.get("name"),
        "model": response_payload.get("model") or model,
        "endpoint": "interactions",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }


def _request_ollama_discussion(
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    request = Request(
        f"{base_url('ollama')}/api/generate",
        data=json.dumps(
            _ollama_discussion_body(model, advisor_input, question, history),
            separators=(",", ":"),
            default=str,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_provider_error("ollama", exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise _network_provider_error("ollama", exc) from exc
    discussion = _parse_discussion_output(
        response_payload.get("response"),
        provider="ollama",
    )
    return {
        **discussion,
        "provider": "ollama",
        "response_id": response_payload.get("created_at"),
        "model": response_payload.get("model") or model,
        "endpoint": "api/generate",
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage({"usage": response_payload}),
    }


def _request_codex_discussion(
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    response_payload = _request_codex_payload(
        "/discussion",
        {
            "model": model,
            "question": question[:2000],
            "history": history,
            "advisor_input": advisor_input,
        },
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    answer = str(response_payload.get("answer") or "").strip()
    if not answer:
        raise RuntimeError("codex_empty_output")
    suggestions = response_payload.get("object_suggestions")
    if not isinstance(suggestions, list):
        raise RuntimeError("codex_invalid_discussion_contract")
    return {
        "answer": answer,
        "object_suggestions": [dict(item) for item in suggestions[:3] if isinstance(item, Mapping)],
        "provider": "codex",
        "response_id": response_payload.get("response_id"),
        "model": response_payload.get("model") or model,
        "endpoint": response_payload.get("endpoint") or "codex-exec",
        "latency_ms": response_payload.get("latency_ms")
        or round((time.monotonic() - started) * 1000.0, 1),
        "usage": _token_usage(response_payload),
    }


def explicit_model_name(provider: str, model: Any = None) -> str:
    selected = str(model or "").strip()
    if selected and selected != "default":
        return OLLAMA_MODEL_ALIASES.get(selected, selected) if provider == "ollama" else selected
    return model_name(provider)


def _dispatch_openai_verdict(
    provider: str,
    advisor_input: dict[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_openai_verdict(
        advisor_input, api_key=api_key, model=model, timeout_seconds=timeout_seconds
    )


def _dispatch_openai_discussion(
    provider: str,
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_openai_discussion(
        advisor_input=advisor_input,
        question=question,
        history=history,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _dispatch_deepseek_verdict(
    provider: str,
    advisor_input: dict[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_deepseek_verdict(
        advisor_input, api_key=api_key, model=model, timeout_seconds=timeout_seconds
    )


def _dispatch_deepseek_discussion(
    provider: str,
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_deepseek_discussion(
        advisor_input=advisor_input,
        question=question,
        history=history,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _dispatch_gemini_verdict(
    provider: str,
    advisor_input: dict[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_gemini_verdict(
        advisor_input, api_key=api_key, model=model, timeout_seconds=timeout_seconds
    )


def _dispatch_gemini_discussion(
    provider: str,
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_gemini_discussion(
        advisor_input=advisor_input,
        question=question,
        history=history,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _dispatch_ollama_verdict(
    provider: str,
    advisor_input: dict[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_ollama_verdict(
        advisor_input, api_key=api_key, model=model, timeout_seconds=timeout_seconds
    )


def _dispatch_ollama_discussion(
    provider: str,
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_ollama_discussion(
        advisor_input=advisor_input,
        question=question,
        history=history,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _dispatch_codex_verdict(
    provider: str,
    advisor_input: dict[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_codex_verdict(
        advisor_input, api_key=api_key, model=model, timeout_seconds=timeout_seconds
    )


def _dispatch_codex_discussion(
    provider: str,
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _request_codex_discussion(
        advisor_input=advisor_input,
        question=question,
        history=history,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class AiProviderManifest:
    name: str
    verdict_request: Callable[..., dict[str, Any]]
    discussion_request: Callable[..., dict[str, Any]]
    requires_api_key: bool


_AI_PROVIDER_MANIFESTS: Mapping[str, AiProviderManifest] = {
    "openai": AiProviderManifest(
        name="openai",
        verdict_request=_dispatch_openai_verdict,
        discussion_request=_dispatch_openai_discussion,
        requires_api_key=True,
    ),
    "deepseek": AiProviderManifest(
        name="deepseek",
        verdict_request=_dispatch_deepseek_verdict,
        discussion_request=_dispatch_deepseek_discussion,
        requires_api_key=True,
    ),
    "gemini": AiProviderManifest(
        name="gemini",
        verdict_request=_dispatch_gemini_verdict,
        discussion_request=_dispatch_gemini_discussion,
        requires_api_key=True,
    ),
    "ollama": AiProviderManifest(
        name="ollama",
        verdict_request=_dispatch_ollama_verdict,
        discussion_request=_dispatch_ollama_discussion,
        requires_api_key=False,
    ),
    "codex": AiProviderManifest(
        name="codex",
        verdict_request=_dispatch_codex_verdict,
        discussion_request=_dispatch_codex_discussion,
        requires_api_key=True,
    ),
}


def provider_manifest(provider: str) -> AiProviderManifest:
    return _AI_PROVIDER_MANIFESTS[normalize_provider(provider)]


def requires_api_key(provider: str) -> bool:
    return provider_manifest(provider).requires_api_key


def request_verdict(
    provider: str,
    advisor_input: dict[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return provider_manifest(provider).verdict_request(
        provider, advisor_input, api_key=api_key, model=model, timeout_seconds=timeout_seconds
    )


def request_discussion(
    provider: str,
    *,
    advisor_input: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return provider_manifest(provider).discussion_request(
        provider,
        advisor_input=advisor_input,
        question=question,
        history=history,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )
