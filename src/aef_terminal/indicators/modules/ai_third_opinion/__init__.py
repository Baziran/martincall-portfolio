from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aef_terminal.domain import Bar, Direction, ScenarioDecision
from aef_terminal.indicators.contracts import normalize_overlay_item
from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    calc_enabled,
)
from aef_terminal.indicators.control_specs import _control, indicator_table_controls
from aef_terminal.indicators.module_contract import (
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSidebarSpec,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import copy_indicator_facts, indicator_fact_payload
from aef_terminal.runtime import overlays as runtime_overlays


AI_THIRD_OPINION_VERSION = "0.3-python"
AI_THIRD_OPINION_LENSES = {"direct", "contrarian"}
AI_THIRD_OPINION_REFRESH_MODES = {"auto", "manual"}


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "ai_third_opinion",
        "AI Third Opinion",
        "advisory market phase opinion from confirmed bars and optional Channel Master context",
        {
            "version": AI_THIRD_OPINION_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "table": None,
        },
        "aiThirdOpinion",
        "aef_terminal.indicators.modules.ai_third_opinion:ai_third_opinion",
        group="post_decision",
        pipeline_stage="post_decision",
        pipeline_order=150,
        candidate_promoter="none",
        paper_tradable=False,
        optional_context=(
            "market_spotlight",
            "trade_setup_engine",
            "option_reversal",
            "martin_carlo",
            "channel_master",
        ),
        input_contract="confirmed-bars+provisional-live-bar+optional-channel-master-context",
        output_contract="ai-third-opinion-v2",
        table_contract="indicator-table-v1",
        score_family="decision",
        empirical_power=0.65,
        usefulness=0.80,
        notes="Advisory context only. Uses exchange-confirmed OHLCV, separately typed provisional live context, and optional Channel Master facts; chart-object suggestions require explicit operator acceptance and never create execution commands.",
        module_type="context",
        state_key="aiThirdOpinion",
        calc_key="aiThirdOpinionCalcEnabled",
        visible_key="aiThirdOpinionVisible",
        chart_control_id="ai-third-opinion-toggle",
        process_control_id="ai-third-opinion-process",
        process_effect_ref="ai_third_opinion_gate",
        api_enabled_key="ai_third_opinion_enabled",
        api_visible_key="ai_third_opinion_visible",
        manager_order=150,
        runtime_order=150,
        default_calc=False,
        default_visible=False,
        renderer_primitives=("box", "line", "table"),
        renderer_placements=("price", "table"),
        renderer_table_label="AI Third",
        renderer_table_header_width=24,
        renderer_table_animation_ref="table_thinking",
        runtime_payload_contract={
            "top_level": ("table", "opinion", "snapshot", "connector"),
            "latest": (
                "action",
                "bias",
                "ai_headline",
                "confidence",
                "connector",
                "contradictions",
                "data_quality",
                "evidence",
                "headline",
                "ai_market_view",
                "invalidation",
                "lens",
                "market_view",
                "pattern",
                "phase",
                "signal",
                "snapshot",
                "source",
                "state",
                "timeframe",
                "ts",
                *INDICATOR_FACT_RUNTIME_FIELDS,
            ),
        },
        runtime_payload_schema_refs=("overlay_contract",),
        controls=(
            *indicator_table_controls("aiThirdOpinion", "ai-third-opinion"),
            _control(
                "lookback",
                "Lookback",
                "number",
                48,
                "aiThirdOpinionLookback",
                element_id="ai-third-opinion-lookback",
                api_key="ai_third_opinion_lookback",
                param_key="lookback",
                minimum=20,
                maximum=160,
                step=4,
                action="load_apply",
            ),
            _control(
                "external",
                "External",
                "toggle",
                False,
                "aiThirdOpinionExternal",
                element_id="ai-third-opinion-external",
                api_key="ai_third_opinion_external",
                param_key="external",
                action="load_apply",
                compact=True,
            ),
            _control(
                "refresh_mode",
                "Refresh",
                "select",
                "auto",
                "aiThirdOpinionRefreshMode",
                element_id="ai-third-opinion-refresh-mode",
                api_key="ai_third_opinion_refresh_mode",
                param_key="refresh_mode",
                scope="global",
                options=("auto", "manual"),
                action="load_apply",
                compact=True,
            ),
            _control(
                "provider",
                "Provider",
                "select",
                "openai",
                "aiThirdOpinionProvider",
                element_id="ai-third-opinion-provider",
                api_key="ai_third_opinion_provider",
                param_key="provider",
                options=("openai", "deepseek", "gemini", "ollama", "codex"),
                action="load_apply",
                effect_ref="ai_third_opinion_model_options",
                compact=True,
            ),
            _control(
                "model",
                "Model",
                "select",
                "default",
                "aiThirdOpinionModel",
                element_id="ai-third-opinion-model",
                api_key="ai_third_opinion_model",
                param_key="model",
                options=(
                    "default",
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
                    "llama3.2",
                    "qwen2.5",
                    "gemma3",
                    "deepseek-r1",
                    "qwen2.5-coder:14b",
                    "deepseek-r1:14b",
                    "deepseek-r1:8b",
                ),
                action="load_apply",
                effect_ref="ai_third_opinion_model_options",
                compact=True,
            ),
            _control(
                "lens",
                "Lens",
                "select",
                "direct",
                "aiThirdOpinionLens",
                element_id="ai-third-opinion-lens",
                api_key="ai_third_opinion_lens",
                param_key="lens",
                options=("direct", "contrarian"),
                action="load_apply",
                compact=True,
            ),
            _control(
                "channel_context",
                "Channels",
                "toggle",
                True,
                "aiThirdOpinionChannelContext",
                element_id="ai-third-opinion-channel-context",
                api_key="ai_third_opinion_channel_context",
                param_key="channel_context",
                action="load_apply",
                compact=True,
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.ai_third_opinion:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.ai_third_opinion:build_params",
    snapshot_enricher_ref=(
        "aef_terminal.indicators.modules.ai_third_opinion.service:"
        "enrich_snapshot_with_ai_third_opinion"
    ),
    router_ref="aef_terminal.indicators.modules.ai_third_opinion.router:create_router",
    ui_js_assets=("client.js",),
    ui_css_assets=("styles.css",),
    sidebar=IndicatorSidebarSpec(
        id="ai",
        title="AI Advisor",
        mount_id="ai-third-opinion-discuss",
        icon_svg=(
            '<path d="M12 3 14 8l5 2-5 2-2 5-2-5-5-2 5-2 2-5Z"/>'
            '<path d="m18 16 .8 2.2L21 19l-2.2.8L18 22l-.8-2.2L15 19l2.2-.8L18 16Z"/>'
        ),
        order=250,
    ),
)


@dataclass(frozen=True)
class AiThirdOpinionParams:
    lookback: int = 48
    external: bool = False
    refresh_mode: str = "auto"
    provider: str = "openai"
    model: str = "default"
    lens: str = "direct"
    channel_context: bool = True


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = ai_third_opinion
    params = ctx.params("ai_third_opinion")
    deps = tuple(
        dep_id for dep_id in ("option_reversal", "martin_carlo") if calc_enabled(ctx, dep_id)
    )
    return IndicatorExecutionSpec(
        id="ai_third_opinion",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate_with_context=lambda dep_results: calculate(
            ctx.confirmed_bars,
            params=params,
            indicator_context={**ctx.indicator_bundle, **dict(dep_results)},
            decision_context=ctx.decision,
            data_quality=ctx.quality,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        promote=False,
        depends_on=deps,
    )


def build_params(
    raw: Mapping[str, Any],
    _defaults: IndicatorDefaults,
) -> AiThirdOpinionParams:
    def _int(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(float(raw.get(name, default)))
        except TypeError, ValueError:
            value = default
        return max(low, min(value, high))

    def _bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    return AiThirdOpinionParams(
        lookback=_int("lookback", AiThirdOpinionParams.lookback, 20, 160),
        external=_bool(raw.get("external"), AiThirdOpinionParams.external),
        refresh_mode=normalize_ai_third_opinion_refresh_mode(raw.get("refresh_mode")),
        provider=str(raw.get("provider") or AiThirdOpinionParams.provider).strip().lower()
        if str(raw.get("provider") or "").strip().lower()
        in {"openai", "deepseek", "gemini", "ollama", "codex"}
        else AiThirdOpinionParams.provider,
        model=str(raw.get("model") or AiThirdOpinionParams.model).strip()[:80]
        or AiThirdOpinionParams.model,
        lens=normalize_ai_third_opinion_lens(raw.get("lens")),
        channel_context=_bool(raw.get("channel_context"), AiThirdOpinionParams.channel_context),
    )


def normalize_ai_third_opinion_lens(value: Any) -> str:
    lens = str(value or AiThirdOpinionParams.lens).strip().lower()
    return lens if lens in AI_THIRD_OPINION_LENSES else AiThirdOpinionParams.lens


def normalize_ai_third_opinion_refresh_mode(value: Any) -> str:
    mode = str(value or AiThirdOpinionParams.refresh_mode).strip().lower()
    return mode if mode in AI_THIRD_OPINION_REFRESH_MODES else AiThirdOpinionParams.refresh_mode


def ai_third_opinion_lens_view(ai_view: str, lens: str) -> str:
    view = str(ai_view or "wait").strip().lower()
    if view not in {"long", "short", "wait"}:
        view = "wait"
    if normalize_ai_third_opinion_lens(lens) != "contrarian":
        return view
    if view == "long":
        return "short"
    if view == "short":
        return "long"
    return "wait"


def ai_third_opinion_lens_line(ai_view: str, market_view: str, lens: str, phase: str) -> str:
    ai = str(ai_view or "wait").strip().upper()
    market = str(market_view or "wait").strip().upper()
    if normalize_ai_third_opinion_lens(lens) == "contrarian":
        return f"AI {ai}->{market}" if ai != market else f"AI {ai}"
    return str(phase or "").replace("_", " ")


def ai_third_opinion_lens_headline(
    ai_view: str, market_view: str, lens: str, pattern: str, fallback: str
) -> str:
    if normalize_ai_third_opinion_lens(lens) != "contrarian":
        return fallback
    ai = str(ai_view or "wait").lower()
    market = str(market_view or "wait").lower()
    if ai in {"long", "short"} and market in {"long", "short"} and ai != market:
        return f"Contrarian {market} lens: AI sees {ai} context ({pattern})"
    return f"Contrarian wait: no clean AI crowd direction ({pattern})"


def ai_third_opinion_object_proposals(
    meta: Mapping[str, Any],
    confirmed_bars: Sequence[Mapping[str, Any]],
    raw_suggestions: Any,
) -> list[dict[str, Any]]:
    """Produce validated advisory overlays and drawing definitions for AI suggestions."""
    instrument_id = meta.get("instrument_id") if isinstance(meta.get("instrument_id"), str) else ""
    route_fingerprint = (
        meta.get("route_fingerprint") if isinstance(meta.get("route_fingerprint"), str) else ""
    )
    timeframe = str(meta.get("timeframe") or "")
    bars = [bar for bar in confirmed_bars if isinstance(bar, Mapping)]
    if not instrument_id or not route_fingerprint or not timeframe or len(bars) < 2:
        return []
    previous_bar = bars[-2]
    latest_bar = bars[-1]
    previous_ts = str(previous_bar.get("ts") or "")
    latest_ts = str(latest_bar.get("ts") or "")
    try:
        parsed_timestamps = [
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in (previous_ts, latest_ts)
        ]
    except ValueError:
        return []
    if any(value.tzinfo is None for value in parsed_timestamps):
        return []
    window_highs = [_safe_float(bar.get("high"), float("nan")) for bar in bars]
    window_lows = [_safe_float(bar.get("low"), float("nan")) for bar in bars]
    finite_highs = [value for value in window_highs if value == value]
    finite_lows = [value for value in window_lows if value == value]
    if not finite_highs or not finite_lows:
        return []
    window_high = max(finite_highs)
    window_low = min(finite_lows)
    window_span = max(window_high - window_low, max(abs(window_high), 1.0) * 0.001)
    low_bound = window_low - window_span * 5.0
    high_bound = window_high + window_span * 5.0
    suggestions = raw_suggestions if isinstance(raw_suggestions, list) else []
    proposals: list[dict[str, Any]] = []
    for index, raw in enumerate(suggestions[:3]):
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in {"target_zone", "price_line"}:
            continue
        direction = str(raw.get("direction") or "neutral").strip().lower()
        if direction not in {"long", "short", "neutral"}:
            direction = "neutral"
        label = str(
            raw.get("label") or ("AI target" if kind == "target_zone" else "AI level")
        ).strip()[:80]
        rationale = str(raw.get("rationale") or "").strip()[:360]
        confidence = max(0.0, min(_safe_float(raw.get("confidence"), 0.0), 1.0))
        try:
            extend_bars = int(raw.get("extend_bars") or 24)
        except TypeError, ValueError:
            extend_bars = 24
        extend_bars = max(4, min(extend_bars, 80))
        fact_fields = indicator_fact_payload(
            scenario="ai_object_suggestion",
            trigger_event={
                "code": "ai_object_suggested",
                "kind": kind,
                "direction": direction,
            },
            quality={"code": "ai_confidence", "value": confidence},
            context={
                "code": "ai_object_rationale",
                "content": rationale or "AI-generated advisory chart object.",
            },
            metrics={
                "kind": kind,
                "direction": direction,
                "confidence": confidence,
                "context_bar_ts": latest_ts,
            },
        )
        tone = "positive" if direction == "long" else "negative" if direction == "short" else "info"
        drawing: dict[str, Any]
        if kind == "target_zone":
            top = _safe_float(raw.get("top"), float("nan"))
            bottom = _safe_float(raw.get("bottom"), float("nan"))
            if (
                top != top
                or bottom != bottom
                or top == bottom
                or not low_bound <= top <= high_bound
                or not low_bound <= bottom <= high_bound
            ):
                continue
            upper = round(max(top, bottom), 6)
            lower = round(min(top, bottom), 6)
            overlay = runtime_overlays.box(
                start_ts=latest_ts,
                end_anchor_ts=latest_ts,
                end_bar_offset=extend_bars,
                top=upper,
                bottom=lower,
                label_text=label,
                tone=tone,
                opacity=0.12,
                role="ai_target_zone_preview",
                direction=direction,
                fact_fields=fact_fields,
            )
            drawing = {
                "type": "zone",
                "points": [
                    {"ts": previous_ts, "price": upper},
                    {"ts": latest_ts, "price": lower},
                ],
                "extendRight": True,
                "style": "solid",
                "width": 2,
                "label": label,
            }
        else:
            price = _safe_float(raw.get("price"), float("nan"))
            if price != price or not low_bound <= price <= high_bound:
                continue
            level = round(price, 6)
            overlay = runtime_overlays.line(
                start_ts=latest_ts,
                price=level,
                label_text=label,
                tone=tone,
                bars_forward=extend_bars,
                width=1.5,
                opacity=0.9,
                role="ai_price_line_preview",
                label_side="above",
                direction=direction,
                fact_fields=fact_fields,
            )
            drawing = {
                "type": "line",
                "points": [
                    {"ts": previous_ts, "price": level},
                    {"ts": latest_ts, "price": level},
                ],
                "extendRight": True,
                "style": "solid",
                "width": 2,
                "label": label,
            }
        signature = "|".join(
            (
                instrument_id,
                route_fingerprint,
                timeframe,
                latest_ts,
                kind,
                label,
                rationale,
                str(drawing["points"]),
                str(index),
            )
        )
        proposal_id = f"aiobj_{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:20]}"
        suggestion_meta = {
            "id": proposal_id,
            "label": label,
            "rationale": rationale,
            "confidence": confidence,
            "direction": direction,
            "context_bar_ts": latest_ts,
        }
        drawing["aiSuggestion"] = suggestion_meta
        proposals.append(
            {
                **suggestion_meta,
                "kind": kind,
                "scope": {
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "timeframe": timeframe,
                },
                "overlay": normalize_overlay_item(overlay, source="ai_third_opinion"),
                "drawing": drawing,
            }
        )
    return proposals


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return default
    return parsed if parsed == parsed else default


def _latest(indicator: Mapping[str, Any] | None) -> Mapping[str, Any]:
    latest = indicator.get("latest") if isinstance(indicator, Mapping) else None
    return latest if isinstance(latest, Mapping) else {}


def _nested_mapping(source: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    cur: Any = source
    for key in keys:
        if not isinstance(cur, Mapping):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, Mapping) else {}


def _trend_score(bars: Sequence[Bar]) -> tuple[float, float]:
    if len(bars) < 2:
        return 0.0, 0.0
    first = bars[0].close
    last = bars[-1].close
    ranges = [max(bar.high - bar.low, 0.0) for bar in bars]
    avg_range = sum(ranges) / max(len(ranges), 1)
    trend = (last - first) / max(avg_range, abs(first) * 0.001, 1e-9)
    closes = [bar.close for bar in bars]
    midpoint = len(closes) // 2
    early = sum(closes[:midpoint]) / max(midpoint, 1)
    late = sum(closes[midpoint:]) / max(len(closes) - midpoint, 1)
    slope = (late - early) / max(avg_range, abs(first) * 0.001, 1e-9)
    return max(-1.0, min(trend / 8.0, 1.0)), max(-1.0, min(slope / 5.0, 1.0))


def _compression_score(bars: Sequence[Bar]) -> float:
    if len(bars) < 12:
        return 0.0
    ranges = [max(bar.high - bar.low, 0.0) for bar in bars]
    recent = sum(ranges[-8:]) / 8.0
    baseline = sum(ranges[:-8]) / max(len(ranges) - 8, 1)
    if baseline <= 0:
        return 0.0
    ratio = recent / baseline
    return max(0.0, min((1.0 - ratio) / 0.45, 1.0))


def _volume_score(bars: Sequence[Bar]) -> float:
    if len(bars) < 12:
        return 0.0
    recent = sum(max(bar.volume, 0.0) for bar in bars[-8:]) / 8.0
    baseline = sum(max(bar.volume, 0.0) for bar in bars[:-8]) / max(len(bars) - 8, 1)
    if baseline <= 0:
        return 0.0
    return max(-1.0, min((recent / baseline - 1.0) / 0.8, 1.0))


def _market_context_signal(indicator_context: Mapping[str, Any]) -> tuple[str, str]:
    market = _latest(indicator_context.get("market_spotlight"))
    context = (
        market.get("market_context") if isinstance(market.get("market_context"), Mapping) else {}
    )
    direction = str(context.get("direction") or market.get("direction") or "").lower()
    state = str(context.get("state") or market.get("state") or "").strip()
    if direction in {"long", "short", "flat"} or state:
        return direction, state
    return "", ""


def _setup_signal(indicator_context: Mapping[str, Any]) -> tuple[str, str]:
    latest = _latest(indicator_context.get("trade_setup_engine"))
    signal = latest.get("signal") if isinstance(latest.get("signal"), Mapping) else {}
    direction = str(signal.get("direction") or latest.get("direction") or "").lower()
    action = str(signal.get("action") or latest.get("action") or latest.get("state") or "").upper()
    return direction, action


def _option_warning(indicator_context: Mapping[str, Any]) -> str:
    latest = _latest(indicator_context.get("option_reversal"))
    state = str(latest.get("state") or "").upper()
    action = str(latest.get("action") or "").upper()
    if state and state not in {"DISABLED", "WAIT"}:
        return state
    return action if action and action not in {"WAIT"} else ""


def _martin_carlo_context(indicator_context: Mapping[str, Any]) -> tuple[str, float, str]:
    forecast = _nested_mapping(_latest(indicator_context.get("martin_carlo")), "forecast")
    if not forecast:
        return "", 0.0, ""
    metrics = forecast.get("metrics") if isinstance(forecast.get("metrics"), Mapping) else {}
    bull = _safe_float(metrics.get("bullish_probability"), 0.0)
    bear = _safe_float(metrics.get("bearish_probability"), 0.0)
    engine = str(forecast.get("engine") or "").lower()
    reason_code = str(forecast.get("engine_reason_code") or "")
    if bull >= bear + 0.08:
        return "long", bull - bear, reason_code or engine
    if bear >= bull + 0.08:
        return "short", bear - bull, reason_code or engine
    return "flat", abs(bull - bear), reason_code or engine


def _decision_context(decision: ScenarioDecision | None) -> tuple[str, str]:
    if decision is None:
        return "", ""
    direction = (
        decision.direction.value
        if isinstance(decision.direction, Direction)
        else str(decision.direction or "")
    )
    return direction.lower(), decision.action.value


def _data_quality_state(data_quality: Mapping[str, Any] | None) -> str:
    if not isinstance(data_quality, Mapping):
        return "confirmed"
    if data_quality.get("signals_ok") is False:
        return "incomplete"
    gap_count = int(_safe_float(data_quality.get("gap_count"), 0.0))
    stale_minutes = _safe_float(data_quality.get("stale_minutes"), 0.0)
    stale_threshold = _safe_float(data_quality.get("stale_threshold_minutes"), 0.0)
    if gap_count > 0 or (stale_threshold > 0 and stale_minutes > stale_threshold):
        return "stale"
    return "confirmed"


def _phase_from_scores(
    *,
    trend: float,
    slope: float,
    compression: float,
    option_warning: str,
) -> str:
    directional = (trend + slope) / 2.0
    if option_warning and abs(directional) >= 0.25:
        return "reversal_risk"
    if compression >= 0.55 and abs(directional) < 0.45:
        return "compression"
    if directional >= 0.32:
        return "trend_up"
    if directional <= -0.32:
        return "trend_down"
    if abs(directional) <= 0.18:
        return "range"
    return "unclear"


def _bias_from_votes(votes: Sequence[str], fallback: str) -> str:
    long_votes = sum(1 for vote in votes if vote == "long")
    short_votes = sum(1 for vote in votes if vote == "short")
    if long_votes > short_votes:
        return "bullish"
    if short_votes > long_votes:
        return "bearish"
    return fallback


def _market_view(phase: str, bias: str, contradictions: Sequence[str]) -> str:
    if phase == "trend_up":
        return "long"
    if phase == "trend_down":
        return "short"
    if phase == "reversal_risk":
        return "wait"
    if bias == "bullish" and len(contradictions) <= 1 and phase not in {"compression", "range"}:
        return "long"
    if bias == "bearish" and len(contradictions) <= 1 and phase not in {"compression", "range"}:
        return "short"
    return "wait"


def _pattern_label(
    phase: str, *, compression: float, option_warning: str, contradictions: Sequence[str]
) -> str:
    if option_warning:
        return "possible false breakout / reversal warning"
    if phase in {"trend_up", "trend_down"}:
        return (
            "early trend formation"
            if abs(compression) < 0.35
            else "trend continuation after squeeze"
        )
    if phase == "compression":
        return "compression before expansion"
    if phase == "range":
        return "range / no clean acceptance"
    if contradictions:
        return "mixed context"
    return "unclear market phase"


def _headline(view: str, pattern: str) -> str:
    if view == "long":
        return f"Look for long context: {pattern}"
    if view == "short":
        return f"Look for short context: {pattern}"
    return f"Wait: {pattern}"


def ai_third_opinion(
    bars: Sequence[Bar],
    *,
    params: AiThirdOpinionParams | None = None,
    indicator_context: Mapping[str, Any] | None = None,
    decision_context: ScenarioDecision | None = None,
    data_quality: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    params = params or AiThirdOpinionParams()
    context = indicator_context if isinstance(indicator_context, Mapping) else {}
    if not bars:
        return {
            "version": AI_THIRD_OPINION_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "table": None,
        }

    lookback_bars = list(bars[-params.lookback :])
    latest_bar = lookback_bars[-1]
    trend, slope = _trend_score(lookback_bars)
    compression = _compression_score(lookback_bars)
    volume = _volume_score(lookback_bars)
    market_direction, market_state = _market_context_signal(context)
    setup_direction, setup_action = _setup_signal(context)
    decision_direction, decision_action = _decision_context(decision_context)
    option_warning = _option_warning(context)
    mc_direction, mc_edge, mc_reason = _martin_carlo_context(context)
    phase = _phase_from_scores(
        trend=trend,
        slope=slope,
        compression=compression,
        option_warning=option_warning,
    )
    fallback_bias = (
        "bullish" if trend + slope > 0.25 else "bearish" if trend + slope < -0.25 else "neutral"
    )
    bias = _bias_from_votes(
        [market_direction, setup_direction, decision_direction, mc_direction],
        fallback_bias,
    )
    evidence = [
        f"confirmed_lookback={len(lookback_bars)}",
        f"trend_score={trend:.2f}",
        f"slope_score={slope:.2f}",
        f"compression={compression:.2f}",
        f"volume_pressure={volume:.2f}",
    ]
    if market_state:
        evidence.append(f"market_spotlight={market_state}")
    if setup_action:
        evidence.append(f"setup_engine={setup_action}")
    if decision_action:
        evidence.append(f"decision={decision_action}")
    if mc_reason:
        evidence.append(f"martin_carlo={mc_reason}")

    contradictions: list[str] = []
    directional_votes = [
        vote
        for vote in [market_direction, setup_direction, decision_direction, mc_direction]
        if vote in {"long", "short"}
    ]
    if "long" in directional_votes and "short" in directional_votes:
        contradictions.append("structured_direction_votes_conflict")
    if option_warning:
        contradictions.append(f"option_reversal={option_warning}")
    if compression >= 0.55 and abs(trend + slope) >= 0.8:
        contradictions.append("compression_against_directional_pressure")
    if mc_direction in {"long", "short"} and (
        (mc_direction == "long" and bias == "bearish")
        or (mc_direction == "short" and bias == "bullish")
    ):
        contradictions.append(f"martin_carlo_{mc_direction}_against_consensus")

    ai_view = _market_view(phase, bias, contradictions)
    view = ai_third_opinion_lens_view(ai_view, params.lens)
    pattern = _pattern_label(
        phase, compression=compression, option_warning=option_warning, contradictions=contradictions
    )
    ai_headline = _headline(ai_view, pattern)
    headline = ai_third_opinion_lens_headline(ai_view, view, params.lens, pattern, ai_headline)

    confidence = (
        0.42
        + min(abs(trend + slope) / 2.0, 0.25)
        + min(abs(volume) * 0.08, 0.08)
        + min(len(evidence) * 0.025, 0.12)
    )
    confidence -= min(len(contradictions) * 0.10, 0.25)
    confidence += min(abs(mc_edge) * 0.08, 0.05)
    confidence = max(0.05, min(confidence, 0.88))

    data_quality_state = _data_quality_state(data_quality)
    auto_external = bool(params.external and params.refresh_mode == "auto")
    connector_status = (
        "pending_external_worker"
        if auto_external
        else "manual_refresh"
        if params.external
        else "local_advisory"
    )
    connector_health = (
        {"state": "pending", "label": "PEND", "detail": "call"}
        if auto_external
        else {"state": "manual", "label": "MAN", "detail": "ask"}
        if params.external
        else {"state": "local", "label": "LOCAL", "detail": "off"}
    )
    connector = {
        "provider": params.provider,
        "status": connector_status,
        "health": connector_health,
        "authoritative": False,
        "external_requested": auto_external,
        "external_enabled": bool(params.external),
        "refresh_mode": params.refresh_mode,
        "channel_context_requested": bool(params.channel_context),
        "model": params.model,
        "lens": params.lens,
        "response_bars_ago": None,
    }
    if auto_external:
        evidence.append("external_ai_worker_not_attached")
    elif params.external:
        evidence.append("external_ai_manual_refresh")

    latest = {
        "ts": latest_bar.ts.isoformat(),
        "timeframe": latest_bar.timeframe,
        "phase": phase,
        "state": phase.upper(),
        "market_view": view,
        "ai_market_view": ai_view,
        "lens": params.lens,
        "headline": headline,
        "ai_headline": ai_headline,
        "pattern": pattern,
        "bias": bias,
        "confidence": round(confidence, 4),
        "action": view.upper(),
        "contradictions": contradictions[:8],
        "invalidation": [
            "phase changes only on confirmed-bar recomputation",
            "treat as advisory until external connector returns a current verdict",
        ],
        "data_quality": data_quality_state,
        "connector": connector,
        **indicator_fact_payload(
            scenario="ai_third_opinion",
            trigger_event={"code": "ai_advisory", "phase": phase, "market_view": view},
            supporting=[
                {"code": "ai_evidence_item", "content": str(item)} for item in evidence[:10]
            ],
            opposing=[
                {"code": "ai_warning_item", "content": str(item)} for item in contradictions[:8]
            ],
            quality={"code": "confidence", "value": confidence},
            context=(
                {
                    "code": "contrarian_trap_watch",
                    "requires": "confirmed_channel_edge_acceptance_or_rejection",
                }
                if normalize_ai_third_opinion_lens(params.lens) == "contrarian"
                else None
            ),
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
                        {"code": "ai_warning_item", "content": str(item)}
                        for item in contradictions[:4]
                    ],
                },
            ],
            metrics={
                "confidence": {"value": confidence * 100.0, "digits": 0, "suffix": "%"},
                "raw_ai_view": str(ai_view).upper(),
                "lens": params.lens,
                "consensus_view": str(view).upper(),
            },
        ),
        "snapshot": {
            "symbol": latest_bar.symbol,
            "source": latest_bar.source,
            "bar_count": len(lookback_bars),
            "last_close": latest_bar.close,
            "scores": {
                "trend": round(trend, 4),
                "slope": round(slope, 4),
                "compression": round(compression, 4),
                "volume": round(volume, 4),
            },
            "context": {
                "market_direction": market_direction,
                "setup_direction": setup_direction,
                "decision_direction": decision_direction,
                "martin_carlo_direction": mc_direction,
                "option_warning": option_warning,
            },
        },
    }
    latest["signal"] = {
        "action": latest["action"],
        "direction": view if view in {"long", "short"} else "flat",
        "score": round(confidence * 100.0, 2),
        "code": f"AI_{phase.upper()}",
        "reason_code": "advisory_context_only",
        "blocked": True,
        "blocked_reason": "advisory_context_only",
    }
    table = {
        "id": "ai_third_opinion",
        "status": connector["status"],
        "ai_status": connector["status"],
        "thinking": auto_external,
        "connector_health": connector["health"],
        "ai_thinking": auto_external,
        "thinking_ref": "ai_third_opinion_discussion",
        "thinking_column": "AI",
        "columns": [
            {
                "cells": [
                    "VIEW",
                    view.upper(),
                    ai_third_opinion_lens_line(ai_view, view, params.lens, phase),
                ]
            },
            {
                "cells": [
                    "CONF",
                    {"kind": "percent", "value": confidence},
                    data_quality_state.upper(),
                ]
            },
            {"cells": ["WHY", pattern, bias.upper()]},
            {
                "cells": [
                    "AI",
                    "PEND" if auto_external else "MAN" if params.external else "LOCAL",
                    "waiting" if auto_external else "manual" if params.external else "local",
                ]
            },
            {"cells": ["HLTH", connector["health"]["label"], connector["health"]["detail"]]},
            {
                "cells": [
                    "CTX",
                    {"kind": "integer", "value": len(evidence), "suffix": " ev"},
                    {"kind": "integer", "value": len(contradictions), "suffix": " warn"},
                ],
            },
            {
                "cells": ["ASK", "SEND", "context"],
                "action": "ai-third-opinion-discuss-send",
            },
        ],
    }
    table_overlay = {
        "type": "table",
        "source": "ai_third_opinion",
        "table": table,
        **copy_indicator_facts(latest),
        "tone": "info",
    }
    return {
        "version": AI_THIRD_OPINION_VERSION,
        "series": [],
        "events": [],
        "latest": latest,
        "opinion": latest,
        "snapshot": latest["snapshot"],
        "connector": connector,
        "table": table,
        "overlays": [table_overlay],
        "signals": [],
    }
