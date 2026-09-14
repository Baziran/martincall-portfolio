from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from . import providers as provider_transport

from aef_terminal.indicators.modules.ai_third_opinion import (
    ai_third_opinion_lens_headline,
    ai_third_opinion_lens_line,
    ai_third_opinion_lens_view,
    ai_third_opinion_object_proposals,
    normalize_ai_third_opinion_lens,
)
from aef_terminal.indicators.domain_facts import indicator_fact_payload


_CACHE_MAX = 64
_VERDICT_CACHE: dict[str, dict[str, Any]] = {}


def _indicator(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    indicators = snapshot.get("indicators")
    if not isinstance(indicators, dict):
        return None
    indicator = indicators.get("ai_third_opinion")
    return indicator if isinstance(indicator, dict) else None


def _latest(indicator: dict[str, Any] | None) -> dict[str, Any]:
    latest = indicator.get("latest") if isinstance(indicator, dict) else None
    return latest if isinstance(latest, dict) else {}


def _tail(items: Any, limit: int) -> list[Any]:
    if not isinstance(items, list):
        return []
    return items[-max(int(limit), 0) :]


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {
        key: source[key] for key in keys if key in source and source[key] not in (None, "", [], {})
    }


def _compact_ts(value: Any) -> str:
    text = str(value or "")
    if "T" in text:
        return text.rsplit("T", 1)[-1].replace("+00:00", "Z")
    return text[-12:]


def _bar_price(bar: dict[str, Any], key: str) -> float | None:
    return _round_number(bar.get(key), 4)


def _compact_bar(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "t": _compact_ts(bar.get("ts")),
            "o": _bar_price(bar, "open"),
            "h": _bar_price(bar, "high"),
            "l": _bar_price(bar, "low"),
            "c": _bar_price(bar, "close"),
            "v": _round_number(bar.get("volume"), 0),
        }.items()
        if value not in (None, "", [], {})
    }


def _confirmed_context_bars(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    bars = snapshot.get("bars")
    if not isinstance(bars, list):
        return []
    return [
        bar
        for bar in bars
        if isinstance(bar, dict)
        and bar.get("closed") is True
        and str(bar.get("state") or "confirmed") == "confirmed"
        and bar.get("authoritative") is not False
        and not bar.get("missing")
        and not bar.get("data_gap")
        and not bar.get("fill_forward")
        and str(bar.get("source") or "") != "gap-placeholder"
    ]


def _live_context_bar(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    bars = snapshot.get("bars")
    if not isinstance(bars, list):
        return None
    for bar in reversed(bars):
        if not isinstance(bar, dict):
            continue
        if (
            bar.get("missing")
            or bar.get("data_gap")
            or bar.get("fill_forward")
            or str(bar.get("source") or "") == "gap-placeholder"
        ):
            continue
        state = str(bar.get("state") or "")
        if bar.get("closed") is False or state in {
            "forming",
            "awaiting_provider_confirmation",
        }:
            return bar
    return None


def _compact_bars(snapshot: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bar in _tail(_confirmed_context_bars(snapshot), limit):
        if not isinstance(bar, dict):
            continue
        out.append(_compact_bar(bar))
    return out


def _round_number(value: Any, digits: int = 4) -> float | None:
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return None
    if parsed != parsed:
        return None
    return round(parsed, digits)


def _numeric_bar(bar: Any) -> dict[str, float] | None:
    if not isinstance(bar, dict):
        return None
    out: dict[str, float] = {}
    for key in ("open", "high", "low", "close", "volume"):
        value = _round_number(bar.get(key), 6)
        if value is None:
            return None
        out[key] = value
    return out


def _candle_summary(snapshot: dict[str, Any], *, limit: int) -> dict[str, Any]:
    raw_bars = _tail(_confirmed_context_bars(snapshot), limit)
    bars = [_numeric_bar(bar) for bar in raw_bars]
    bars = [bar for bar in bars if bar is not None]
    if not bars:
        return {"count": 0}
    first = bars[0]
    last = bars[-1]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    ranges = [max(bar["high"] - bar["low"], 0.0) for bar in bars]
    high = max(highs)
    low = min(lows)
    span = max(high - low, 0.0)
    last_close = last["close"]
    prev_close = bars[-2]["close"] if len(bars) > 1 else first["open"]
    avg_range = sum(ranges) / len(ranges)
    avg_volume = sum(volumes) / len(volumes)
    up_closes = sum(1 for index in range(1, len(closes)) if closes[index] > closes[index - 1])
    down_closes = sum(1 for index in range(1, len(closes)) if closes[index] < closes[index - 1])
    close_position = ((last_close - low) / span) if span else 0.5
    return {
        key: value
        for key, value in {
            "count": len(bars),
            "first_t": _compact_ts(raw_bars[0].get("ts")),
            "last_t": _compact_ts(raw_bars[-1].get("ts")),
            "last_close": _round_number(last_close),
            "window_high": _round_number(high),
            "window_low": _round_number(low),
            "window_range": _round_number(span),
            "change": _round_number(last_close - first["open"]),
            "change_pct": _round_number(((last_close / first["open"]) - 1.0) * 100.0, 3)
            if first["open"]
            else None,
            "close_position_pct": _round_number(close_position * 100.0, 1),
            "avg_range": _round_number(avg_range),
            "last_range": _round_number(ranges[-1]),
            "last_body": _round_number(last["close"] - last["open"]),
            "last_vs_prev_close": _round_number(last_close - prev_close),
            "avg_volume": _round_number(avg_volume, 0),
            "last_volume_vs_avg_pct": _round_number(
                ((last["volume"] / avg_volume) - 1.0) * 100.0, 1
            )
            if avg_volume
            else None,
            "up_closes": up_closes,
            "down_closes": down_closes,
        }.items()
        if value not in (None, "", [], {})
    }


def _price_relation(
    level_price: float | None, current_price: float | None
) -> tuple[str | None, float | None]:
    if level_price is None or current_price is None:
        return None, None
    distance = _round_number(level_price - current_price)
    if distance is None:
        return None, None
    if abs(distance) <= 1e-9:
        return "at", 0.0
    return ("above" if distance > 0 else "below"), distance


def _compact_channel_level(level: Any, *, current_price: float | None = None) -> dict[str, Any]:
    if not isinstance(level, dict):
        return {}
    level_price = _round_number(level.get("price"))
    relation, distance_points = _price_relation(level_price, current_price)
    return {
        key: value
        for key, value in {
            "label": level.get("label"),
            "role": level.get("role"),
            "price": level_price,
            "relation_to_price": relation,
            "distance_points": distance_points,
            "distance_atr": _round_number(level.get("distance_atr"), 3),
            "side": level.get("side"),
            "touch": bool(level.get("touch")),
            "reclaim": bool(level.get("reclaim")),
            "inside": bool(level.get("inside")),
            "direction": level.get("direction"),
            "primary_path": level.get("primary_path"),
            "no_fade": (level.get("no_fade") if isinstance(level.get("no_fade"), bool) else None),
            "break_score": _round_number(level.get("break_score"), 1),
            "bounce_score": _round_number(level.get("bounce_score"), 1),
            "raw_break_score": _round_number(
                level.get("raw_break_score"),
                1,
            ),
            "target": _round_number(level.get("target")),
        }.items()
        if value not in (None, "", [], {})
    }


def _compact_manual_channels(
    snapshot: dict[str, Any],
    *,
    enabled: bool,
    current_price: float | None = None,
    current_ts: str = "",
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}
    indicators = snapshot.get("indicators")
    context = indicators.get("channel_master") if isinstance(indicators, dict) else None
    if not isinstance(context, dict):
        return {"enabled": True, "active": False, "count": 0, "channels": [], "nearest": None}
    context_price = _round_number(context.get("price"))
    reference_price = current_price if current_price is not None else context_price
    channels: list[dict[str, Any]] = []
    for channel in _tail(context.get("channels"), 2):
        if not isinstance(channel, dict):
            continue
        levels = channel.get("levels") if isinstance(channel.get("levels"), list) else []
        sorted_levels = sorted(
            (level for level in levels if isinstance(level, dict)),
            key=lambda level: abs(_round_number(level.get("distance_atr"), 3) or 999.0),
        )[:3]
        channels.append(
            {
                key: value
                for key, value in {
                    "id": str(channel.get("id") or "")[:32],
                    "inside": bool(channel.get("inside")),
                    "position_pct": _round_number(channel.get("position_pct"), 3),
                    "lower": _round_number(channel.get("lower")),
                    "upper": _round_number(channel.get("upper")),
                    "base": _round_number(channel.get("base")),
                    "offset": _round_number(channel.get("offset")),
                    "slope_per_confirmed_bar": _round_number(
                        channel.get("slope_per_confirmed_bar"),
                        6,
                    ),
                    "impulse_strength": _round_number(channel.get("impulse_strength"), 2),
                    "nearest_levels": [
                        _compact_channel_level(level, current_price=reference_price)
                        for level in sorted_levels
                    ],
                }.items()
                if value not in (None, "", [], {})
            }
        )
    nearest = _compact_channel_level(context.get("nearest"), current_price=reference_price)
    if nearest and isinstance(context.get("nearest"), dict):
        nearest["channel_id"] = str(context["nearest"].get("channel_id") or "")[:32]
    return {
        "enabled": True,
        "active": bool(context.get("active")),
        "count": int(context.get("count") or len(channels)),
        "price": reference_price,
        "price_ts": current_ts,
        "distance_reference": "last_confirmed_close"
        if current_price is not None
        else "channel_context_price",
        "channel_observed_price": context_price,
        "channel_observed_ts": str(
            context.get("price_ts") or context.get("ts") or context.get("analysis_ts") or ""
        ),
        "nearest": nearest or None,
        "channels": channels,
    }


def _advisor_input(
    snapshot: dict[str, Any],
    indicator: dict[str, Any],
    *,
    include_live_bar: bool = False,
) -> dict[str, Any]:
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    latest = _latest(indicator)
    connector = latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    channel_context_enabled = connector.get("channel_context_requested") is not False
    provider = provider_transport.provider_name(indicator)
    context_limit = provider_transport.context_bar_limit(provider)
    confirmed_context_bars = _tail(
        _confirmed_context_bars(snapshot),
        context_limit,
    )
    candle_summary = _candle_summary(snapshot, limit=context_limit)
    live = _live_context_bar(snapshot) if include_live_bar else None
    live_close = _round_number(live.get("close")) if isinstance(live, dict) else None
    last_confirmed_close = _round_number(candle_summary.get("last_close"))
    last_confirmed_ts = (
        str(confirmed_context_bars[-1].get("ts") or "") if confirmed_context_bars else ""
    )
    expected_close = str(live.get("expected_close") or "") if isinstance(live, dict) else ""
    seconds_to_close: int | None = None
    if expected_close:
        try:
            closes_at = datetime.fromisoformat(expected_close.replace("Z", "+00:00"))
            if closes_at.tzinfo is not None:
                seconds_to_close = max(
                    int((closes_at.astimezone(UTC) - datetime.now(tz=UTC)).total_seconds()),
                    0,
                )
        except ValueError:
            expected_close = ""
    live_bar = (
        {
            "provisional": True,
            "authoritative": False,
            "is_confirmed": False,
            "closed": False,
            "state": str(live.get("state") or "forming"),
            "source": str(live.get("source") or ""),
            "preview_kind": str(live.get("preview_kind") or "live_candle"),
            "ts": str(live.get("ts") or ""),
            "current_price": live_close,
            "next_close_at": expected_close or None,
            "seconds_to_close": seconds_to_close,
            "ohlcv": _compact_bar(live),
            "vs_last_confirmed_close": (
                _round_number(live_close - last_confirmed_close)
                if live_close is not None and last_confirmed_close is not None
                else None
            ),
        }
        if isinstance(live, dict)
        else None
    )
    base_timeframe = str(meta.get("timeframe") or "")
    higher_target = {
        "1m": "5m",
        "5m": "15m",
        "15m": "60m",
    }.get(base_timeframe)
    higher_context = (
        {
            "source": "confirmed_base_bars",
            "base_timeframe": base_timeframe,
            "target_timeframe": higher_target,
            "synthetic_bars": False,
            "confirmed_bar_count": len(confirmed_context_bars),
            "short_window": _candle_summary(snapshot, limit=20),
            "medium_window": _candle_summary(snapshot, limit=50),
            "full_window": candle_summary,
        }
        if higher_target
        else None
    )
    advisor_input = {
        "meta": _pick(
            meta,
            ("symbol", "provider_symbol", "timeframe", "source", "confirmed_bar_count"),
        ),
        "bars": _compact_bars(snapshot, limit=context_limit),
        "candle_summary": candle_summary,
        "manual_channels": _compact_manual_channels(
            snapshot,
            enabled=channel_context_enabled,
            current_price=last_confirmed_close,
            current_ts=last_confirmed_ts,
        ),
    }
    if include_live_bar:
        advisor_input["live_bar"] = live_bar
        advisor_input["higher_context"] = higher_context
    return advisor_input


def _bar_timestamps(snapshot: dict[str, Any]) -> list[str]:
    timestamps: list[str] = []
    bars = snapshot.get("bars")
    if not isinstance(bars, list):
        return timestamps
    for bar in bars:
        if isinstance(bar, dict) and bar.get("ts") is not None:
            timestamps.append(str(bar["ts"]))
    return timestamps


def _latest_bar_ts(snapshot: dict[str, Any]) -> str:
    timestamps = _bar_timestamps(snapshot)
    return timestamps[-1] if timestamps else ""


def _response_bars_ago(snapshot: dict[str, Any], response_bar_ts: Any) -> int | None:
    response_ts = str(response_bar_ts or "")
    if not response_ts:
        return None
    timestamps = _bar_timestamps(snapshot)
    try:
        return max(len(timestamps) - 1 - timestamps.index(response_ts), 0)
    except ValueError:
        return None


def _cache_key(provider: str, model: str, advisor_input: dict[str, Any]) -> str:
    raw = json.dumps(
        {"provider": provider, "model": model, "input": advisor_input},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _normalize_strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:180] for item in value if str(item).strip()][:limit]


def _normalized_verdict(
    verdict: dict[str, Any], *, default_timeframe: str, default_data_quality: str
) -> dict[str, Any]:
    phase = str(verdict.get("phase") or "unclear")
    if phase not in {"trend_up", "trend_down", "range", "compression", "reversal_risk", "unclear"}:
        phase = "unclear"
    bias = str(verdict.get("bias") or "neutral")
    if bias not in {"bullish", "bearish", "neutral"}:
        bias = "neutral"
    market_view = str(verdict.get("market_view") or "")
    if market_view not in {"long", "short", "wait"}:
        market_view = (
            "long"
            if bias == "bullish" and phase == "trend_up"
            else "short"
            if bias == "bearish" and phase == "trend_down"
            else "wait"
        )
    headline = str(verdict.get("headline") or "").strip()[:140]
    pattern = str(verdict.get("pattern") or "").strip()[:80]
    if not pattern:
        pattern = "market context review"
    if not headline:
        headline = (
            f"Look for {market_view} context: {pattern}"
            if market_view in {"long", "short"}
            else f"Wait: {pattern}"
        )
    try:
        confidence = float(verdict.get("confidence", 0.0))
    except TypeError, ValueError:
        confidence = 0.0
    data_quality = str(verdict.get("data_quality") or default_data_quality or "confirmed")
    if data_quality not in {"confirmed", "stale", "incomplete"}:
        data_quality = (
            default_data_quality
            if default_data_quality in {"confirmed", "stale", "incomplete"}
            else "confirmed"
        )
    return {
        "phase": phase,
        "bias": bias,
        "market_view": market_view,
        "headline": headline,
        "pattern": pattern,
        "confidence": max(0.0, min(confidence, 1.0)),
        "timeframe": str(verdict.get("timeframe") or default_timeframe or ""),
        "evidence": _normalize_strings(verdict.get("evidence"), 8),
        "contradictions": _normalize_strings(verdict.get("contradictions"), 8),
        "invalidation": _normalize_strings(verdict.get("invalidation"), 6),
        "data_quality": data_quality,
        "_openai": verdict.get("_openai") if isinstance(verdict.get("_openai"), dict) else {},
    }


def _ai_status_column(connector_status: str, bars_ago: int | None) -> dict[str, Any]:
    if connector_status == "ready":
        line1 = "ON"
    elif connector_status == "cache":
        line1 = "CACHE"
    elif connector_status == "missing_api_key":
        return {"cells": ["AI", "OFF", "no key"]}
    elif connector_status in {
        "error",
        "insufficient_quota",
        "rate_limited",
        "auth_error",
        "forbidden",
        "configuration_error",
        "network_error",
        "model_missing",
    }:
        return {"cells": ["AI", "ERR", "stale"]}
    else:
        line1 = str(connector_status or "PEND").upper()[:10]
    line2: str | dict[str, Any] = (
        {"kind": "integer", "value": bars_ago, "suffix": " bars"}
        if isinstance(bars_ago, int)
        else "-"
    )
    return {"cells": ["AI", line1, line2]}


def _connector_health(status: str, *, cache_hit: bool = False) -> dict[str, str]:
    if status == "ready":
        return {"state": "connected", "label": "OK", "detail": "live"}
    if status == "cache" or cache_hit:
        return {"state": "cache", "label": "CACHE", "detail": "prev"}
    if status == "missing_api_key":
        return {"state": "missing_api_key", "label": "NO KEY", "detail": "env"}
    typed_errors = {
        "insufficient_quota": {"state": "insufficient_quota", "label": "NO $", "detail": "quota"},
        "rate_limited": {"state": "rate_limited", "label": "RATE", "detail": "limit"},
        "auth_error": {"state": "auth_error", "label": "AUTH", "detail": "key"},
        "forbidden": {"state": "forbidden", "label": "DENY", "detail": "access"},
        "configuration_error": {
            "state": "configuration_error",
            "label": "CONFIG",
            "detail": "endpoint",
        },
        "network_error": {"state": "network_error", "label": "NET", "detail": "fail"},
        "model_missing": {"state": "model_missing", "label": "MODEL", "detail": "miss"},
    }
    if status in typed_errors:
        return typed_errors[status]
    if status in {"pending_external_worker", "pending", "thinking", "requesting"}:
        return {"state": "pending", "label": "PEND", "detail": "call"}
    if status == "manual_refresh":
        return {"state": "manual", "label": "MAN", "detail": "ask"}
    if status == "error":
        return {"state": "error", "label": "ERR", "detail": "fail"}
    return {"state": status or "unknown", "label": str(status or "UNK").upper()[:7], "detail": "-"}


def _connector_status_for_exception(error: BaseException) -> str:
    if isinstance(error, provider_transport.AiProviderError):
        return error.status
    return "error"


def _health_column(health: dict[str, str]) -> dict[str, Any]:
    return {"cells": ["HLTH", health.get("label") or "UNK", health.get("detail") or "-"]}


def _lens_name(indicator: dict[str, Any] | None) -> str:
    latest = _latest(indicator)
    connector = latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    return normalize_ai_third_opinion_lens(connector.get("lens") or latest.get("lens"))


def _table_for_verdict(
    verdict: dict[str, Any],
    connector_status: str,
    bars_ago: int | None,
    *,
    health: dict[str, str] | None = None,
    lens: str = "direct",
) -> dict[str, Any]:
    openai_meta = verdict.get("_openai") if isinstance(verdict.get("_openai"), dict) else {}
    usage = openai_meta.get("usage") if isinstance(openai_meta.get("usage"), dict) else {}
    total_tokens = usage.get("total_tokens")
    token_line: dict[str, Any] = (
        {"kind": "integer", "value": total_tokens, "suffix": " tok"}
        if isinstance(total_tokens, int)
        else {"kind": "integer", "value": len(verdict["evidence"]), "suffix": " ev"}
    )
    token_subline: dict[str, Any] = (
        {
            "parts": (
                {"kind": "integer", "value": usage.get("input_tokens"), "empty": "-"},
                {"kind": "integer", "value": usage.get("output_tokens"), "empty": "-"},
            ),
            "separator": "/",
        }
        if isinstance(total_tokens, int)
        else {"kind": "integer", "value": len(verdict["contradictions"]), "suffix": " warn"}
    )
    health_payload = health or _connector_health(connector_status)
    ai_view = str(verdict["market_view"])
    market_view = ai_third_opinion_lens_view(ai_view, lens)
    return {
        "id": "ai_third_opinion",
        "title": "AI Third Opinion",
        "status": connector_status,
        "ai_status": connector_status,
        "thinking": False,
        "connector_health": health_payload,
        "ai_thinking": False,
        "thinking_column": "AI",
        "columns": [
            {
                "cells": [
                    "VIEW",
                    market_view.upper(),
                    ai_third_opinion_lens_line(ai_view, market_view, lens, str(verdict["phase"])),
                ]
            },
            {
                "cells": [
                    "CONF",
                    {"kind": "percent", "value": verdict["confidence"]},
                    str(verdict["data_quality"]).upper(),
                ]
            },
            {"cells": ["WHY", str(verdict["pattern"]), str(verdict["bias"]).upper()]},
            _ai_status_column(connector_status, bars_ago),
            _health_column(health_payload),
            {"cells": ["TOK", token_line, token_subline]},
            {
                "cells": ["ASK", "SEND", "context"],
                "action": "ai-third-opinion-discuss-send",
            },
        ],
    }


def _fact_fields_for_verdict(
    verdict: dict[str, Any],
    connector_status: str,
    bars_ago: int | None,
    provider: str = provider_transport.DEFAULT_PROVIDER,
    *,
    lens: str = "direct",
) -> dict[str, Any]:
    ai_view = str(verdict.get("market_view") or "wait")
    market_view = ai_third_opinion_lens_view(ai_view, lens)
    normalized_lens = normalize_ai_third_opinion_lens(lens)
    commentary = f"System consensus lens: generalized systems would likely watch {ai_view.upper()}."
    notes = ""
    if normalized_lens == "contrarian":
        commentary = f"Contrarian lens: stronger liquidity systems may be positioned against that crowd view; displayed view {market_view.upper()}."
        notes = "Trap watch: inside user channels, random rotation can be stop-run behavior; wait for confirmed acceptance or rejection at channel edges."
    evidence = verdict.get("evidence") if isinstance(verdict.get("evidence"), list) else []
    contradictions = (
        verdict.get("contradictions") if isinstance(verdict.get("contradictions"), list) else []
    )
    openai_meta = verdict.get("_openai") if isinstance(verdict.get("_openai"), dict) else {}
    usage = openai_meta.get("usage") if isinstance(openai_meta.get("usage"), dict) else {}
    health = _connector_health(connector_status)
    return indicator_fact_payload(
        scenario="ai_third_opinion",
        trigger_event={
            "code": "ai_advisory",
            "phase": str(verdict.get("phase") or "unknown"),
            "market_view": market_view,
        },
        supporting=[{"code": "ai_evidence_item", "content": str(item)} for item in evidence[:8]],
        opposing=[{"code": "ai_warning_item", "content": str(item)} for item in contradictions[:8]],
        context=[
            {"code": "ai_consensus_context", "content": commentary},
            *(
                [
                    {
                        "code": "trap_watch_inside_user_channels",
                        "content": notes,
                        "requires": "confirmed_channel_edge_acceptance_or_rejection",
                    }
                ]
                if notes
                else []
            ),
        ],
        fact_groups=[
            {
                "kind": "ai_evidence",
                "items": [
                    {"code": "ai_evidence_item", "content": str(item)} for item in evidence[:4]
                ],
            },
            {
                "kind": "ai_warning",
                "items": [
                    {"code": "ai_warning_item", "content": str(item)} for item in contradictions[:3]
                ],
            },
        ],
        metrics={
            "pattern": verdict.get("pattern") or "unclear market phase",
            "raw_ai_view": ai_view.upper(),
            "lens": normalized_lens.upper(),
            "displayed_view": market_view.upper(),
            "bars_ago": bars_ago
            if isinstance(bars_ago, int) and connector_status in {"ready", "cache"}
            else None,
            "connector_status": connector_status,
            "connector_health": health["label"],
            "connector_detail": health["detail"],
            "provider": provider.upper(),
            "token_total": usage.get("total_tokens") if usage else None,
            "token_input": usage.get("input_tokens") if usage else None,
            "token_output": usage.get("output_tokens") if usage else None,
        },
    )


def _merge_verdict(
    indicator: dict[str, Any],
    verdict: dict[str, Any],
    *,
    connector_status: str,
    cache_hit: bool,
    snapshot: dict[str, Any],
) -> None:
    latest = indicator.setdefault("latest", {})
    if not isinstance(latest, dict):
        latest = {}
        indicator["latest"] = latest
    previous_connector = (
        latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    )
    openai_meta = verdict.get("_openai") if isinstance(verdict.get("_openai"), dict) else {}
    provider = str(
        openai_meta.get("provider")
        or previous_connector.get("provider")
        or provider_transport.DEFAULT_PROVIDER
    )
    lens = _lens_name(indicator)
    ai_view = str(verdict["market_view"])
    market_view = ai_third_opinion_lens_view(ai_view, lens)
    headline = ai_third_opinion_lens_headline(
        ai_view, market_view, lens, str(verdict["pattern"]), str(verdict["headline"])
    )
    response_bar_ts = openai_meta.get("response_bar_ts") or _latest_bar_ts(snapshot)
    bars_ago = _response_bars_ago(snapshot, response_bar_ts)
    health = _connector_health(connector_status, cache_hit=cache_hit)
    fact_fields = _fact_fields_for_verdict(verdict, connector_status, bars_ago, provider, lens=lens)
    connector = {
        "provider": provider,
        "status": connector_status,
        "health": health,
        "authoritative": False,
        "external_requested": True,
        "channel_context_requested": previous_connector.get("channel_context_requested")
        is not False,
        "cache_hit": bool(cache_hit),
        "model": openai_meta.get("model") or provider_transport.model_name(provider, indicator),
        "lens": lens,
        "endpoint": openai_meta.get("endpoint") or "responses",
        "response_id": openai_meta.get("response_id"),
        "latency_ms": openai_meta.get("latency_ms"),
        "usage": openai_meta.get("usage") if isinstance(openai_meta.get("usage"), dict) else {},
        "response_bar_ts": response_bar_ts,
        "response_bars_ago": bars_ago,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    latest.update(
        {
            "phase": verdict["phase"],
            "state": str(verdict["phase"]).upper(),
            "market_view": market_view,
            "ai_market_view": ai_view,
            "lens": lens,
            "headline": headline,
            "ai_headline": verdict["headline"],
            "pattern": verdict["pattern"],
            "bias": verdict["bias"],
            "confidence": round(float(verdict["confidence"]), 4),
            "timeframe": verdict["timeframe"],
            "contradictions": verdict["contradictions"],
            "invalidation": verdict["invalidation"],
            "data_quality": verdict["data_quality"],
            "connector": connector,
            **fact_fields,
            "source": provider,
        }
    )
    latest["action"] = market_view.upper()
    latest["signal"] = {
        "action": latest["action"],
        "direction": market_view if market_view in {"long", "short"} else "flat",
        "score": round(float(verdict["confidence"]) * 100.0, 2),
        "code": f"AI_{str(verdict['phase']).upper()}",
        "reason_code": "advisory_context_only",
        "blocked": True,
        "blocked_reason": "advisory_context_only",
    }
    table = _table_for_verdict(verdict, connector_status, bars_ago, health=health, lens=lens)
    indicator["table"] = table
    indicator["opinion"] = latest
    indicator["connector"] = connector
    overlays = indicator.get("overlays") if isinstance(indicator.get("overlays"), list) else []
    table_overlay = {
        "type": "table",
        "source": "ai_third_opinion",
        "table": table,
        **fact_fields,
        "accent": "rgba(82, 168, 255, 0.82)",
        "contract": "overlay-contract-v1",
    }
    indicator["overlays"] = [
        item for item in overlays if not (isinstance(item, dict) and item.get("type") == "table")
    ] + [table_overlay]
    status = indicator.get("status")
    if isinstance(status, dict):
        status["state_code"] = "advisory_ready" if connector_status == "ready" else "advisory_cache"
        status["health"] = "ok"
        status["reason_code"] = f"ai_advisory_{connector_status}"
        status["trigger_event"] = {
            "code": "ai_advisory_ready" if connector_status == "ready" else "ai_advisory_cache",
            "provider": provider,
        }
        status["last_error"] = None


def _set_connector_error(
    indicator: dict[str, Any], status: str, message: str, *, provider: str, model: str
) -> None:
    latest = indicator.setdefault("latest", {})
    if not isinstance(latest, dict):
        latest = {}
        indicator["latest"] = latest
    lens = _lens_name(indicator)
    health = _connector_health(status)
    connector = {
        "provider": provider,
        "status": status,
        "health": health,
        "authoritative": False,
        "external_requested": True,
        "channel_context_requested": latest.get("connector", {}).get("channel_context_requested")
        is not False
        if isinstance(latest.get("connector"), dict)
        else True,
        "model": model,
        "lens": lens,
        "message": message[:240],
        "response_bars_ago": None,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    latest["connector"] = connector
    indicator["connector"] = connector
    verdict = {
        "phase": latest.get("phase") or "unclear",
        "bias": latest.get("bias") or "neutral",
        "market_view": latest.get("ai_market_view") or latest.get("market_view") or "wait",
        "headline": latest.get("ai_headline")
        or latest.get("headline")
        or "AI advisory unavailable",
        "pattern": latest.get("pattern") or "connector unavailable",
        "confidence": float(latest.get("confidence") or 0.0),
        "evidence": latest.get("evidence") if isinstance(latest.get("evidence"), list) else [],
        "contradictions": [message],
        "data_quality": latest.get("data_quality") or "confirmed",
    }
    table = _table_for_verdict(
        verdict,
        status,
        None,
        health=health,
        lens=lens,
    )
    fact_fields = indicator_fact_payload(
        scenario="ai_advisory_unavailable",
        trigger_event={"code": status, "provider": provider},
        context={"code": "connector_error", "content": message[:180]},
        metrics={
            "provider": provider.upper(),
            "connector_status": status,
            "connector_health": health["label"],
            "connector_detail": health["detail"],
        },
    )
    latest.update(fact_fields)
    indicator["table"] = table
    overlays = indicator.get("overlays") if isinstance(indicator.get("overlays"), list) else []
    table_overlay = {
        "type": "table",
        "source": "ai_third_opinion",
        "table": table,
        **fact_fields,
        "accent": "rgba(82, 168, 255, 0.82)",
        "contract": "overlay-contract-v1",
    }
    indicator["overlays"] = [
        item for item in overlays if not (isinstance(item, dict) and item.get("type") == "table")
    ] + [table_overlay]
    status_payload = indicator.get("status")
    if isinstance(status_payload, dict):
        status_payload["state_code"] = "blocked_context" if status == "missing_api_key" else "stale"
        status_payload["health"] = "blocked" if status == "missing_api_key" else "stale"
        status_payload["reason_code"] = status
        status_payload["trigger_event"] = {
            "code": "ai_advisory_unavailable",
            "reason_code": status,
            "provider": provider,
        }
        status_payload["last_error"] = message[:180]


def enrich_snapshot_with_ai_third_opinion(snapshot: dict[str, Any]) -> dict[str, Any]:
    indicator = _indicator(snapshot)
    if indicator is None:
        return snapshot
    latest = _latest(indicator)
    connector = latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    if not connector.get("external_requested"):
        return snapshot
    provider = provider_transport.provider_name(indicator)
    model = provider_transport.model_name(provider, indicator)
    backoff = provider_transport.provider_backoff_remaining(provider)
    if backoff is not None:
        status, remaining_seconds, detail = backoff
        _set_connector_error(
            indicator,
            status,
            provider_transport.provider_backoff_message(
                provider, status, remaining_seconds, detail
            ),
            provider=provider,
            model=model,
        )
        return snapshot
    api_key = provider_transport.api_key(provider)
    if provider_transport.requires_api_key(provider) and not api_key:
        _set_connector_error(
            indicator,
            "missing_api_key",
            f"{provider_transport.api_key_name(provider)} is not configured.",
            provider=provider,
            model=model,
        )
        return snapshot
    advisor_input = _advisor_input(snapshot, indicator)
    key = _cache_key(provider, model, advisor_input)
    cached = _VERDICT_CACHE.get(key)
    if isinstance(cached, dict):
        provider_transport.clear_provider_backoff(provider)
        _merge_verdict(
            indicator, cached, connector_status="cache", cache_hit=True, snapshot=snapshot
        )
        return snapshot
    try:
        verdict = provider_transport.request_verdict(
            provider,
            advisor_input,
            api_key=api_key,
            model=model,
            timeout_seconds=provider_transport.timeout_seconds(provider),
        )
        normalized = _normalized_verdict(
            verdict,
            default_timeframe=str(
                latest.get("timeframe") or (snapshot.get("meta") or {}).get("timeframe") or ""
            ),
            default_data_quality=str(latest.get("data_quality") or "confirmed"),
        )
        normalized.setdefault("_openai", {})["response_bar_ts"] = _latest_bar_ts(snapshot)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        status = _connector_status_for_exception(exc)
        if status in {"insufficient_quota", "rate_limited"}:
            provider_transport.note_provider_backoff(provider, status, message)
        _set_connector_error(indicator, status, message, provider=provider, model=model)
        return snapshot
    _VERDICT_CACHE[key] = normalized
    if len(_VERDICT_CACHE) > _CACHE_MAX:
        for old_key in list(_VERDICT_CACHE)[: len(_VERDICT_CACHE) - _CACHE_MAX]:
            _VERDICT_CACHE.pop(old_key, None)
    _merge_verdict(
        indicator, normalized, connector_status="ready", cache_hit=False, snapshot=snapshot
    )
    provider_transport.clear_provider_backoff(provider)
    return snapshot


def test_ai_third_opinion_provider(
    provider: Any = provider_transport.DEFAULT_PROVIDER, model: Any = None
) -> dict[str, Any]:
    provider_name = provider_transport.normalize_provider(provider)
    model_name = provider_transport.explicit_model_name(provider_name, model)
    api_key = provider_transport.api_key(provider_name)
    if provider_transport.requires_api_key(provider_name) and not api_key:
        health = _connector_health("missing_api_key")
        return {
            "ok": False,
            "status": "missing_api_key",
            "provider": provider_name,
            "model": model_name,
            "health": health,
            "message": f"{provider_transport.api_key_name(provider_name)} is not configured.",
        }
    advisor_input = {
        "meta": {"symbol": "CONNECTION_TEST", "timeframe": "diagnostic"},
        "bars": [],
        "candle_summary": {"count": 0},
        "manual_channels": {"enabled": False},
    }
    try:
        discussion = provider_transport.request_discussion(
            provider_name,
            advisor_input=advisor_input,
            question="Connection test. Reply with one short sentence starting with OK.",
            history=[],
            api_key=api_key,
            model=model_name,
            timeout_seconds=provider_transport.timeout_seconds(provider_name),
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        status = _connector_status_for_exception(exc)
        return {
            "ok": False,
            "status": status,
            "provider": provider_name,
            "model": model_name,
            "health": _connector_health(status),
            "message": message[:1000],
        }
    return {
        "ok": True,
        "status": "ready",
        "provider": discussion.get("provider") or provider_name,
        "model": discussion.get("model") or model_name,
        "endpoint": discussion.get("endpoint"),
        "response_id": discussion.get("response_id"),
        "latency_ms": discussion.get("latency_ms"),
        "usage": discussion.get("usage") if isinstance(discussion.get("usage"), dict) else {},
        "health": _connector_health("ready"),
        "answer": str(discussion.get("answer") or "")[:500],
    }


def discuss_ai_third_opinion(
    snapshot: dict[str, Any],
    question: str,
    history: Any = None,
) -> dict[str, Any]:
    clean_question = str(question or "").strip()
    if not clean_question:
        return {"ok": False, "status": "empty_question", "message": "Question is empty."}
    indicator = _indicator(snapshot)
    if indicator is None:
        return {
            "ok": False,
            "status": "indicator_missing",
            "message": "AI Third Opinion is not present in the supplied snapshot.",
        }
    provider = provider_transport.provider_name(indicator)
    model = provider_transport.model_name(provider, indicator)
    backoff = provider_transport.provider_backoff_remaining(provider)
    if backoff is not None:
        status, remaining_seconds, detail = backoff
        return {
            "ok": False,
            "status": status,
            "provider": provider,
            "model": model,
            "message": provider_transport.provider_backoff_message(
                provider, status, remaining_seconds, detail
            ),
        }
    api_key = provider_transport.api_key(provider)
    if provider_transport.requires_api_key(provider) and not api_key:
        return {
            "ok": False,
            "status": "missing_api_key",
            "message": f"{provider_transport.api_key_name(provider)} is not configured.",
        }

    advisor_input = _advisor_input(
        snapshot,
        indicator,
        include_live_bar=True,
    )
    compact_history = provider_transport.compact_discussion_history(history)
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    bars = snapshot.get("bars") if isinstance(snapshot.get("bars"), list) else []
    latest = _latest(indicator)
    latest_connector = latest.get("connector") if isinstance(latest.get("connector"), dict) else {}
    try:
        discussion = provider_transport.request_discussion(
            provider,
            advisor_input=advisor_input,
            question=clean_question,
            history=compact_history,
            api_key=api_key,
            model=model,
            timeout_seconds=provider_transport.timeout_seconds(provider),
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        status = _connector_status_for_exception(exc)
        if status in {"insufficient_quota", "rate_limited"}:
            provider_transport.note_provider_backoff(provider, status, message)
        return {
            "ok": False,
            "status": status,
            "provider": provider,
            "model": model,
            "message": message,
        }
    object_suggestions = ai_third_opinion_object_proposals(
        meta,
        _confirmed_context_bars(snapshot),
        discussion.get("object_suggestions"),
    )
    live_bar = _live_context_bar(snapshot)
    return {
        "ok": True,
        "status": "ready",
        "answer": discussion["answer"],
        "provider": discussion.get("provider") or provider,
        "model": discussion.get("model"),
        "endpoint": discussion.get("endpoint"),
        "response_id": discussion.get("response_id"),
        "latency_ms": discussion.get("latency_ms"),
        "usage": discussion.get("usage") if isinstance(discussion.get("usage"), dict) else {},
        "object_suggestions": object_suggestions,
        "context": {
            "instrument_id": meta.get("instrument_id"),
            "route_fingerprint": meta.get("route_fingerprint"),
            "symbol": meta.get("symbol"),
            "timeframe": meta.get("timeframe"),
            "latest_bar_ts": bars[-1].get("ts") if bars and isinstance(bars[-1], dict) else None,
            "latest_confirmed_bar_ts": _latest_bar_ts({"bars": _confirmed_context_bars(snapshot)}),
            "live_bar_ts": live_bar.get("ts") if live_bar else None,
            "verdict_source": latest.get("source") or latest_connector.get("status"),
            "market_view": latest.get("market_view"),
            "ai_market_view": latest.get("ai_market_view") or latest.get("market_view"),
            "lens": latest.get("lens") or latest_connector.get("lens") or "direct",
            "phase": latest.get("phase"),
        },
    }
