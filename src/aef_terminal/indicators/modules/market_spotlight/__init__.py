from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    shared_market_series,
    shared_vix_context,
    shared_vsa_facts,
    unavailable_indicator_result,
)
from aef_terminal.features.market_context import (
    impulse_poi_context,
    liquidity_void_context,
    micro_range_context,
    provider_opening_range_context,
    poi_entry_gate_context,
    strategy_banner_context,
    side_veto_context,
    trap_shift_context,
    true_count,
    vix_risk_context,
)
from aef_terminal.features.market_series import MarketSeriesBlock, build_market_series_block
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.features.vsa import VsaFact, VsaParams, vsa_facts
from aef_terminal.features.vsa_classify import (
    VSA_ABSORPTION_CODES,
    VSA_IMPULSE_CODES,
    VSA_REVERSAL_LONG_CODES,
    VSA_REVERSAL_SHORT_CODES,
    combined_reversal_long,
    combined_reversal_short,
    latest_vsa_context_item,
    vsa_event_code,
    vsa_series_from_facts,
)
from aef_terminal.indicators.control_specs import _control, indicator_table_controls
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import indicator_fact_payload
from aef_terminal.runtime import pine
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import float_or_none, round_optional as _round
from aef_terminal.runtime.mtf import confirmed_intrabar_parent_preview


MARKET_SPOTLIGHT_VERSION = "0.2-python-confirmed-1m-preview"
MARKET_CONTEXT_VERSION = "0.2-python-market-context"


def _first_finite(*values: Any) -> float:
    for value in values:
        number = float_or_none(value)
        if number is not None:
            return number
    raise ValueError("market spotlight price fact must contain a finite value")


MARKET_CONTEXT_ROW_FIELDS = (
    "code",
    "continuation_pattern",
    "direction",
    "flow",
    "flow_bias",
    "flow_state",
    "lock",
    "market_context",
    "pullback_reentry",
    "rvol",
    "score",
    "sentiment_role",
    "setup",
    "side_lock",
    "state",
    "state_direction",
    "strategy_phase",
    "ts",
    "vsa_context_code",
    "advisor_visible",
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "market_spotlight",
        "Market Spotlight",
        "macro market context banner: state/flow/lock/setup",
        {
            "version": MARKET_SPOTLIGHT_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "table": None,
            "market_context": None,
            "overlays": [],
        },
        "marketSpotlight",
        "aef_terminal.indicators.modules.market_spotlight:market_spotlight",
        pipeline_order=30,
        candidate_promoter="none",
        candidate_score_floor_ref="zero",
        paper_tradable=False,
        score_family="volume",
        empirical_power=0.95,
        usefulness=1.10,
        module_type="context",
        shared_context_refs=("vsa_facts", "vix_context", "market_series"),
        state_key="marketSpotlight",
        calc_key="marketSpotlightCalcEnabled",
        visible_key="marketSpotlightVisible",
        chart_control_id="market-spotlight-toggle",
        process_control_id="market-spotlight-process",
        derived_state_refs=("table_position_state",),
        table_setting_id="market-spotlight-table-position",
        api_enabled_key="market_spotlight_enabled",
        manager_order=30,
        runtime_order=30,
        table_contract="indicator-table-v1",
        renderer_primitives=("table", "line"),
        renderer_placements=("table", "price"),
        renderer_table_label="Market Spotlight",
        renderer_table_header_width=34,
        renderer_table_icon="beacon",
        renderer_table_animation_ref="market_spotlight_state_beam",
        overlay_layer="levels",
        overlay_filter_ref="generic_overlay_filter",
        runtime_payload_contract={
            "top_level": ("sentiment_role",),
            "latest": MARKET_CONTEXT_ROW_FIELDS,
            "series": MARKET_CONTEXT_ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "overlays": {"limit": 40, "fields": "compact_contract"},
        },
        optional_context=("impulse_fib",),
        default_calc=False,
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe="1m",
                history_bars=64,
                role="lower_timeframe_confirmation",
            ),
        ),
        controls=(
            *indicator_table_controls("marketSpotlight", "market-spotlight"),
            _control(
                "tableStyle",
                "Style",
                "select",
                "compact",
                "marketSpotlightTableStyle",
                element_id="market-spotlight-table-style",
                api_key="market_spotlight_table_style",
                param_key="table_style",
                options=("compact", "full"),
                action="load_apply",
            ),
            _control(
                "animate",
                "Animate",
                "toggle",
                True,
                "marketSpotlightAnimate",
                element_id="market-spotlight-animate",
                action="render",
                effect_ref="market_spotlight_animation",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.market_spotlight:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.market_spotlight:build_params",
    ui_js_assets=("client.js",),
    ui_css_assets=("styles.css",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = market_spotlight
    params = ctx.params("market_spotlight")
    confirmed_micro_preview_bars = confirmed_intrabar_parent_preview(
        ctx.confirmed_bars,
        ctx.confirmed_bar_context.get("1m", ()),
        ctx.confirmed_bar_context_quality.get("1m"),
        "1m",
    )
    vix_context = shared_vix_context(ctx)
    return IndicatorExecutionSpec(
        id="market_spotlight",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate_with_context=lambda dependency_context: calculate(
            ctx.confirmed_bars,
            profile=ctx.instrument_profile,
            params=params,
            features=ctx.features,
            facts=shared_vsa_facts(ctx),
            vix_context=vix_context,
            market_series=shared_market_series(ctx),
            vwap_session=ctx.vwap_session,
            indicator_context={**ctx.indicator_bundle, **dict(dependency_context)},
        ),
        preview_calculate_with_context=(
            (
                lambda dependency_context: calculate(
                    confirmed_micro_preview_bars,
                    profile=ctx.instrument_profile,
                    params=params,
                    features={},
                    facts=None,
                    vix_context=vix_context,
                    market_series=None,
                    vwap_session=ctx.vwap_session,
                    indicator_context={
                        **ctx.indicator_bundle,
                        **dict(dependency_context),
                    },
                )
            )
            if confirmed_micro_preview_bars
            else None
        ),
        preview_event_ts=(
            confirmed_micro_preview_bars[-1].ts.isoformat() if confirmed_micro_preview_bars else ""
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        promote=False,
    )


@dataclass(frozen=True)
class MarketSpotlightParams(VsaParams):
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    ema_pullback_len: int = DEFAULT_INDICATOR_SETTINGS.ema.pullback
    ema_trend_len: int = 50
    ema_magnet_len: int = DEFAULT_INDICATOR_SETTINGS.ema.magnet
    pullback_rvol: float = 1.05
    indian_adx_min: float = 24.0
    table_style: str = "compact"


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> MarketSpotlightParams:
    return MarketSpotlightParams(
        atr_len=defaults.atr_len,
        ema_pullback_len=defaults.ema.pullback,
        ema_magnet_len=defaults.ema.magnet,
        pullback_rvol=float(
            raw.get(
                "pullback_rvol",
                MarketSpotlightParams.pullback_rvol,
            )
        ),
        indian_adx_min=float(
            raw.get(
                "indian_adx_min",
                MarketSpotlightParams.indian_adx_min,
            )
        ),
        table_style=str(raw.get("table_style", MarketSpotlightParams.table_style)),
    )


def _empty() -> dict[str, Any]:
    return {
        "version": MARKET_SPOTLIGHT_VERSION,
        "series": [],
        "events": [],
        "latest": None,
        "table": None,
        "market_context": None,
        "overlays": [],
    }


def _market_series_block(
    bars: Sequence[Bar],
    params: MarketSpotlightParams,
    market_series: MarketSeriesBlock | None,
    vwap_session: ProviderSessionReset,
) -> MarketSeriesBlock:
    if market_series is not None:
        return market_series
    return build_market_series_block(
        list(bars),
        ema_pullback_len=params.ema_pullback_len,
        ema_trend_len=params.ema_trend_len,
        ema_magnet_len=params.ema_magnet_len,
        atr_len=params.atr_len,
        vwap_session=vwap_session,
    )


def _normalized_table_style(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return "full" if normalized in {"full", "tv", "tradingview", "trading_view"} else "compact"


def _continuation_pattern_from_context(
    indicator_context: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    impulse = (indicator_context or {}).get("impulse_fib")
    if not isinstance(impulse, dict):
        return {"active": False}
    pattern = impulse.get("continuation_pattern")
    if not isinstance(pattern, dict) or not pattern.get("active"):
        latest = impulse.get("latest") if isinstance(impulse.get("latest"), dict) else {}
        pattern = latest.get("continuation_pattern") if isinstance(latest, dict) else None
    return pattern if isinstance(pattern, dict) and pattern.get("active") else {"active": False}


def _continuation_pattern_with_confirmed_invalidation(
    pattern: dict[str, Any], latest_bar: Bar
) -> dict[str, Any]:
    if not pattern.get("active") or not latest_bar.closed:
        return pattern
    invalidation = float_or_none(pattern.get("invalidation"))
    if invalidation is None:
        return pattern
    direction = str(pattern.get("direction") or "flat")
    close = float(latest_bar.close)
    failed = (direction == "short" and close > invalidation) or (
        direction == "long" and close < invalidation
    )
    if not failed:
        return pattern
    reason_codes = [str(item) for item in pattern.get("reason_codes", []) if item]
    reason_codes.append("confirmed_close_beyond_invalidation")
    typed_pattern = {key: value for key, value in pattern.items() if key != "reason"}
    return {
        **typed_pattern,
        "phase": "failed",
        "invalidated": True,
        "invalidated_by": "confirmed_close",
        "invalidated_close": _round(close),
        "reason_codes": reason_codes,
    }


def _pullback_reentry_context(
    *,
    continuation_pattern: dict[str, Any],
    side_gate_context: dict[str, Any],
    latest_ema20: float,
    latest_vwap: float,
) -> dict[str, Any]:
    if not continuation_pattern.get("active"):
        return {"active": False}
    correction_direction = str(continuation_pattern.get("direction") or "flat")
    if correction_direction == "short":
        reentry_direction = "long"
        blocked = bool(side_gate_context.get("veto_long"))
        reclaim = max(
            _first_finite(continuation_pattern.get("invalidation"), 0.0),
            latest_ema20,
            latest_vwap,
        )
        stop = _first_finite(
            continuation_pattern.get("break_level"),
            continuation_pattern.get("pattern_low"),
            reclaim,
        )
    elif correction_direction == "long":
        reentry_direction = "short"
        blocked = bool(side_gate_context.get("veto_short"))
        reclaim = min(
            _first_finite(continuation_pattern.get("invalidation"), latest_ema20),
            latest_ema20,
            latest_vwap,
        )
        stop = _first_finite(
            continuation_pattern.get("break_level"),
            continuation_pattern.get("pattern_high"),
            reclaim,
        )
    else:
        return {"active": False}
    phase = "watch" if blocked else "arm"
    if str(continuation_pattern.get("phase") or "") == "failed":
        phase = "arm"
    return {
        "active": True,
        "mode": "pullback_reentry",
        "direction": reentry_direction,
        "correction_direction": correction_direction,
        "phase": phase,
        "blocked": blocked,
        "trigger": _round(reclaim),
        "stop_reference": _round(stop),
        "source_pattern": str(continuation_pattern.get("type") or "flag"),
        "directive": "wait_reclaim" if blocked else "prepare_reclaim_entry",
    }


@dataclass(frozen=True, slots=True)
class _SpotlightDecisionFacts:
    latest_bar: Bar
    flow_bias: int
    flow_conflict: bool
    side_lock: str
    vsa_context_code: str
    context_code: str
    latest_rvol: float
    fuel_rvol: float
    latest_vwap: float
    latest_ema20: float
    runaway_day_up: bool
    runaway_day_dn: bool
    vwap_upper_2_ride: bool
    vwap_lower_2_ride: bool
    vwap_upper_2_reversal_valid: bool
    vwap_lower_2_reversal_valid: bool
    reversal_long_valid: bool
    reversal_short_valid: bool
    counter_long_unlock: bool
    counter_short_unlock: bool
    vsa_fuel_latest: bool
    continuation_active: bool
    liquidity: dict[str, Any]
    trap: dict[str, Any]
    micro: dict[str, Any]
    vix: dict[str, Any]
    poi: dict[str, Any]
    side_gate: dict[str, Any]
    entry_gate: dict[str, Any]
    continuation_pattern: dict[str, Any]
    pullback_reentry: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MarketStateStage:
    state: str
    direction: str
    reason_code: str
    attention: bool


def _market_state_stage(facts: _SpotlightDecisionFacts) -> _MarketStateStage:
    if facts.continuation_active:
        return _MarketStateStage(
            "continuation_pattern",
            str(facts.continuation_pattern.get("direction") or "flat"),
            "continuation_pattern_active",
            False,
        )
    if facts.trap.get("ready"):
        return _MarketStateStage(
            "trap", str(facts.trap.get("direction") or "flat"), "trap_ready", False
        )
    if facts.micro.get("break"):
        return _MarketStateStage(
            "micro_break",
            str(facts.micro.get("direction") or "flat"),
            "micro_range_break",
            True,
        )
    if facts.vix.get("risk_off") and facts.vwap_upper_2_reversal_valid:
        return _MarketStateStage("vix_risk", "short", "risk_off_upper_extension", True)
    if facts.reversal_long_valid:
        return _MarketStateStage("reversal_attempt", "long", "vsa_reversal_long", False)
    if facts.reversal_short_valid:
        return _MarketStateStage("reversal_attempt", "short", "vsa_reversal_short", False)
    if facts.vsa_context_code in VSA_ABSORPTION_CODES:
        return _MarketStateStage("absorption", "flat", "vsa_absorption", True)
    if facts.vsa_context_code in VSA_IMPULSE_CODES:
        direction = "long" if facts.flow_bias >= 0 else "short"
        return _MarketStateStage("trend_initiation", direction, "vsa_impulse", False)
    if abs(facts.flow_bias) <= 1:
        return _MarketStateStage("balance", "flat", "mixed_flow", True)
    direction = "long" if facts.flow_bias > 0 else "short"
    return _MarketStateStage("directional_flow", direction, "directional_flow", True)


def _countertrend_entry_direction(facts: _SpotlightDecisionFacts) -> str:
    if facts.side_gate.get("veto_long") and (
        facts.reversal_long_valid
        or facts.micro.get("direction") == "long"
        or facts.liquidity.get("reclaim_long")
    ):
        return "long"
    if facts.side_gate.get("veto_short") and (
        facts.reversal_short_valid
        or facts.micro.get("direction") == "short"
        or facts.liquidity.get("reclaim_short")
    ):
        return "short"
    return ""


def _poi_entry_stage(facts: _SpotlightDecisionFacts) -> tuple[str, str, str, bool] | None:
    if facts.poi.get("active") and not facts.poi.get("hot"):
        return (
            "poi_wait",
            "poi_not_hot",
            str(facts.poi.get("direction") or "flat"),
            True,
        )
    if facts.poi.get("hot"):
        return "poi_hot", "poi_hot", str(facts.poi.get("direction") or "flat"), False
    return None


def _entry_status_stage(facts: _SpotlightDecisionFacts) -> dict[str, Any]:
    state = "context_wait"
    detail_code = "await_context"
    trigger: float | None = None
    relation: str | None = None
    direction = "flat"
    phase = "context"
    attention = True
    countertrend_direction = _countertrend_entry_direction(facts)
    poi_stage = _poi_entry_stage(facts)
    if facts.flow_conflict:
        state, detail_code, phase, attention = "context_conflict", "no_force", "blocked", False
    elif facts.continuation_active:
        direction = str(facts.continuation_pattern.get("direction") or "flat")
        pattern_phase = str(facts.continuation_pattern.get("phase") or "watch")
        state = "continuation_triggered" if pattern_phase == "triggered" else "continuation_watch"
        detail_code = "break_level"
        trigger = _first_finite(
            facts.continuation_pattern.get("break_level"),
            facts.latest_bar.low if direction == "short" else facts.latest_bar.high,
        )
        relation = "below" if direction == "short" else "above" if direction == "long" else None
        phase, attention = "watch", pattern_phase != "triggered"
    elif facts.pullback_reentry.get("active") and facts.pullback_reentry.get("phase") == "arm":
        direction = str(facts.pullback_reentry.get("direction") or "flat")
        state, detail_code = "pullback_reentry_arm", "reclaim_trigger"
        trigger = _first_finite(facts.pullback_reentry.get("trigger"), facts.latest_bar.close)
        relation, phase, attention = "reclaim", "watch", True
    elif countertrend_direction:
        state, detail_code, direction, phase = (
            "countertrend_watch",
            "need_reclaim" if countertrend_direction == "long" else "need_reject",
            countertrend_direction,
            "watch",
        )
    elif facts.micro.get("watch") or facts.micro.get("break"):
        direction = str(facts.micro.get("direction") or "flat")
        state = "micro_break_watch" if facts.micro.get("break") else "micro_watch"
        detail_code = "micro_break" if facts.micro.get("break") else "micro_watch"
        phase = "watch"
    elif poi_stage is not None:
        state, detail_code, direction, attention = poi_stage
    elif (
        facts.runaway_day_up
        or facts.runaway_day_dn
        or facts.vwap_upper_2_ride
        or facts.vwap_lower_2_ride
        or facts.vsa_context_code in VSA_IMPULSE_CODES
    ):
        state, detail_code = "pullback_wait", "no_chase"
        direction = "long" if facts.flow_bias > 0 else "short" if facts.flow_bias < 0 else "flat"
    elif facts.reversal_long_valid or facts.reversal_short_valid:
        state, detail_code, phase = "reversal_trigger_watch", "reclaim_or_reject", "watch"
        direction = "long" if facts.reversal_long_valid else "short"
    elif abs(facts.flow_bias) <= 1:
        state, detail_code, phase, attention = "no_trade", "balance", "blocked", False
    return {
        "state": state,
        "detail_code": detail_code,
        "trigger": _round(trigger),
        "relation": relation,
        "direction": direction,
        "attention": attention,
        "gate": facts.entry_gate,
        "phase": phase,
        "actionable": False,
        "generates_signal": False,
    }


def _risk_stage(facts: _SpotlightDecisionFacts) -> dict[str, Any]:
    blocks: tuple[str, ...] = ()
    if facts.liquidity.get("price_in_void"):
        state, detail_code, tone = "void", "liquidity_void", "warn"
    elif facts.runaway_day_up or facts.vwap_upper_2_ride or facts.side_gate.get("veto_short"):
        state, detail_code, tone, blocks = (
            "no_short",
            "runaway_or_side_veto",
            "danger",
            ("short",),
        )
    elif facts.runaway_day_dn or facts.vwap_lower_2_ride or facts.side_gate.get("veto_long"):
        state, detail_code, tone, blocks = (
            "no_long",
            "runaway_or_side_veto",
            "danger",
            ("long",),
        )
    elif facts.vix.get("risk_off"):
        state = "vix"
        detail_code = "vix_upper_reversal" if facts.vwap_upper_2_reversal_valid else "vix_risk"
        tone = "warn"
    elif facts.trap.get("ready"):
        state, detail_code, tone = "countertrend_trap", "trap_ready", "warn"
    elif facts.vsa_fuel_latest or facts.latest_rvol >= facts.fuel_rvol:
        state, detail_code, tone = "exhaustion", "elevated_rvol", "warn"
    elif facts.micro.get("watch") or facts.micro.get("break"):
        state, detail_code, tone = "micro", "micro_range", "warn"
    elif abs(facts.flow_bias) <= 1:
        state, detail_code, tone = "chop", "mixed_flow", "warn"
    else:
        state, detail_code, tone = "clear", "within_limits", "clear"
    return {
        "state": state,
        "detail_code": detail_code,
        "tone": tone,
        "blocks": list(blocks),
        "rvol": _round(facts.latest_rvol),
    }


def _poi_invalidation_stage(
    facts: _SpotlightDecisionFacts,
) -> tuple[float, str, str, str] | None:
    direction = str(facts.poi.get("direction") or "flat")
    if facts.poi.get("active") and direction == "long" and facts.poi.get("bottom") is not None:
        return float(facts.poi["bottom"]), "long", "below", "poi"
    if facts.poi.get("active") and direction == "short" and facts.poi.get("top") is not None:
        return float(facts.poi["top"]), "short", "above", "poi"
    return None


def _invalidation_stage(
    facts: _SpotlightDecisionFacts,
    *,
    entry_direction: str,
) -> dict[str, Any]:
    level: float | None = facts.latest_vwap
    source = "vwap"
    direction = entry_direction
    relation = "reclaim"
    blocks_side: str | None = None
    pattern_direction = str(facts.continuation_pattern.get("direction") or "flat")
    poi_stage = _poi_invalidation_stage(facts)
    if facts.continuation_active and facts.continuation_pattern.get("invalidation") is not None:
        level = float(facts.continuation_pattern["invalidation"])
        direction = pattern_direction
        relation = "below" if direction == "long" else "above" if direction == "short" else "beyond"
        source = "continuation_pattern"
    elif poi_stage is not None:
        level, direction, relation, source = poi_stage
    elif (
        (facts.micro.get("watch") or facts.micro.get("break"))
        and facts.micro.get("direction") == "long"
        and facts.micro.get("low") is not None
    ):
        level, direction, relation, source = float(facts.micro["low"]), "long", "below", "micro"
    elif (
        (facts.micro.get("watch") or facts.micro.get("break"))
        and facts.micro.get("direction") == "short"
        and facts.micro.get("high") is not None
    ):
        level, direction, relation, source = float(facts.micro["high"]), "short", "above", "micro"
    elif facts.trap.get("ready") and facts.trap.get("level") is not None:
        level = float(facts.trap["level"])
        direction, relation, source = str(facts.trap.get("direction") or "flat"), "beyond", "trap"
    elif entry_direction == "long":
        level, relation, source = facts.latest_ema20, "below", "ema20"
    elif entry_direction == "short":
        level, relation, source = facts.latest_ema20, "above", "ema20"
    elif facts.runaway_day_up or facts.vwap_upper_2_ride:
        level, direction, relation, source, blocks_side = (
            None,
            "short",
            "blocked",
            "side_lock",
            "short",
        )
    elif facts.runaway_day_dn or facts.vwap_lower_2_ride:
        level, direction, relation, source, blocks_side = (
            None,
            "long",
            "blocked",
            "side_lock",
            "long",
        )
    return {
        "level": _round(level),
        "direction": direction,
        "source": source,
        "relation": relation,
        "blocks_side": blocks_side,
    }


def market_spotlight(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    params: MarketSpotlightParams | None = None,
    features: dict[str, Any] | None = None,
    facts: Sequence[VsaFact] | None = None,
    vix_context: dict[str, Any] | None = None,
    market_series: MarketSeriesBlock | None = None,
    vwap_session: ProviderSessionReset | None = None,
    indicator_context: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    params = params or MarketSpotlightParams()
    features = features or {}
    if not bars:
        return _empty()
    if vwap_session is None or not vwap_session.available:
        return unavailable_indicator_result(INDICATOR_MODULE.spec.empty_result, vwap_session)

    facts = list(facts) if facts is not None else vsa_facts(bars, params, vwap_session=vwap_session)
    block = _market_series_block(bars, params, market_series, vwap_session)
    ema20 = block.ema_pullback
    ema50 = block.ema_trend
    ema233 = block.ema_magnet
    vwap = block.vwap
    vwap_sigma, vwap_upper_1, vwap_lower_1, vwap_upper_2, vwap_lower_2 = (
        block.vwap_sigma,
        block.vwap_upper_1,
        block.vwap_lower_1,
        block.vwap_upper_2,
        block.vwap_lower_2,
    )
    atr_values = block.atr_sma
    adx_values = block.adx
    mtf_context = block.mtf_context

    latest_bar = bars[-1]
    latest_atr = max(float(atr_values[-1]), 0.000001)
    latest_ema20 = float(ema20[-1])
    latest_ema50 = float(ema50[-1])
    latest_ema233 = float(ema233[-1])
    latest_vwap = float(vwap[-1])
    latest_vwap_sigma = float(vwap_sigma[-1])
    latest_upper_1 = float(vwap_upper_1[-1])
    latest_lower_1 = float(vwap_lower_1[-1])
    latest_upper_2 = float(vwap_upper_2[-1])
    latest_lower_2 = float(vwap_lower_2[-1])
    latest_anatomy = pine.bar_anatomy(latest_bar)

    vsa_series = vsa_series_from_facts(facts, importance_floor=0.0)
    vsa_latest = vsa_series[-1] if vsa_series else {}
    vsa_ctx = latest_vsa_context_item(vsa_series)
    vsa_context_code = vsa_event_code(vsa_ctx)
    context_code = vsa_context_code or str(vsa_latest.get("code") or "")
    latest_rvol = float(vsa_latest.get("rvol") or 0.0)
    vsa_fuel_latest = bool(vsa_latest.get("fuel") or vsa_ctx.get("fuel"))

    recent_vsa = vsa_series[-12:]
    long_pressure = sum(1 for item in recent_vsa if str(item.get("direction") or "") == "long")
    short_pressure = sum(1 for item in recent_vsa if str(item.get("direction") or "") == "short")
    flow_bias = long_pressure - short_pressure

    ema20_base = ema20[-6] if len(ema20) >= 6 else ema20[0]
    vwap_base = vwap[-4] if len(vwap) >= 4 else vwap[0]
    ema20_slope_atr = (latest_ema20 - float(ema20_base)) / latest_atr
    vwap_slope_atr = (latest_vwap - float(vwap_base)) / latest_atr
    vwap_slope_bull = vwap_slope_atr > 0
    vwap_slope_bear = vwap_slope_atr < 0
    vwap_slope_strength_ok = abs(vwap_slope_atr) >= 0.03

    trend_up_now = latest_bar.close > latest_ema20 >= latest_ema233 and ema20_slope_atr >= -0.02
    trend_dn_now = latest_bar.close < latest_ema20 <= latest_ema233 and ema20_slope_atr <= 0.02
    full_bull_now = latest_bar.close > latest_ema50 and latest_bar.close > latest_vwap
    full_bear_now = latest_bar.close < latest_ema50 and latest_bar.close < latest_vwap

    m15_bull_score = int(mtf_context["bull_score"])
    m15_bear_score = int(mtf_context["bear_score"])
    vwap_trend_gate_bull = (
        vwap_slope_bull
        and vwap_slope_strength_ok
        and (adx_values[-1] > params.indian_adx_min or m15_bull_score >= 4)
    )
    vwap_trend_gate_bear = (
        vwap_slope_bear
        and vwap_slope_strength_ok
        and (adx_values[-1] > params.indian_adx_min or m15_bear_score >= 4)
    )
    flow_bias += 2 if trend_up_now else -2 if trend_dn_now else 0
    flow_bias += 1 if ema20_slope_atr > 0.10 else -1 if ema20_slope_atr < -0.10 else 0
    flow_bias = int(pine.clamp(flow_bias, -5, 5))
    flow_conflict = bool(long_pressure > 0 and short_pressure > 0 and abs(flow_bias) <= 1)

    vwap_band_ready = latest_vwap_sigma > 0.000001
    vwap_touch_upper_2 = [
        vwap_band_ready and bar.high >= up for bar, up in zip(bars, vwap_upper_2, strict=False)
    ]
    vwap_touch_lower_2 = [
        vwap_band_ready and bar.low <= dn for bar, dn in zip(bars, vwap_lower_2, strict=False)
    ]
    vwap_upper_2_ride = (
        true_count(vwap_touch_upper_2, 8) >= 3
        and latest_bar.close > latest_upper_2
        and vwap_slope_bull
    )
    vwap_lower_2_ride = (
        true_count(vwap_touch_lower_2, 8) >= 3
        and latest_bar.close < latest_lower_2
        and vwap_slope_bear
    )
    vwap_upper_2_reversal_valid = (
        vwap_band_ready
        and latest_bar.high >= latest_upper_2
        and latest_bar.close < latest_bar.open
        and latest_anatomy.upper_share >= 0.35
        and not vwap_upper_2_ride
    )
    vwap_lower_2_reversal_valid = (
        vwap_band_ready
        and latest_bar.low <= latest_lower_2
        and latest_bar.close > latest_bar.open
        and latest_anatomy.lower_share >= 0.35
        and not vwap_lower_2_ride
    )
    trend_day_bull = full_bull_now and bool(mtf_context["filter_bull_ok"]) and vwap_slope_bull
    trend_day_bear = full_bear_now and bool(mtf_context["filter_bear_ok"]) and vwap_slope_bear
    opening_range_context = provider_opening_range_context(
        bars,
        provider_session=vwap_session,
    )
    adx_runaway_ok = bool(adx_values[-1] >= max(params.indian_adx_min, 28.0))
    or_no_retest_up = bool(
        opening_range_context.get("ready") and opening_range_context.get("no_retest_up")
    )
    or_no_retest_down = bool(
        opening_range_context.get("ready") and opening_range_context.get("no_retest_down")
    )
    runaway_day_up = (
        trend_day_bull
        and flow_bias >= 3
        and latest_bar.close > latest_upper_1
        and not vwap_upper_2_reversal_valid
        and (adx_runaway_ok or or_no_retest_up)
    )
    runaway_day_dn = (
        trend_day_bear
        and flow_bias <= -3
        and latest_bar.close < latest_lower_1
        and not vwap_lower_2_reversal_valid
        and (adx_runaway_ok or or_no_retest_down)
    )
    stretch_up = (
        latest_bar.close > latest_ema20 + latest_atr * 1.35
        or latest_bar.close > latest_ema233 + latest_atr * 3.8
    )
    stretch_dn = (
        latest_bar.close < latest_ema20 - latest_atr * 1.35
        or latest_bar.close < latest_ema233 - latest_atr * 3.8
    )
    contra_short_locked = (
        (trend_up_now or trend_day_bull or vwap_upper_2_ride or runaway_day_up)
        and (ema20_slope_atr > 0.08 or stretch_up or vwap_upper_2_ride or runaway_day_up)
    ) and vsa_context_code not in VSA_REVERSAL_SHORT_CODES
    contra_long_locked = (
        (trend_dn_now or trend_day_bear or vwap_lower_2_ride or runaway_day_dn)
        and (ema20_slope_atr < -0.08 or stretch_dn or vwap_lower_2_ride or runaway_day_dn)
    ) and vsa_context_code not in VSA_REVERSAL_LONG_CODES

    liquidity_context = liquidity_void_context(
        bars, atr_values, [fact.rvol for fact in facts], min_rvol=max(0.95, params.pullback_rvol)
    )
    trap_context = trap_shift_context(
        bars,
        atr_values,
        [fact.rvol for fact in facts],
        full_bull=full_bull_now,
        full_bear=full_bear_now,
        mtf_direction=str(mtf_context["direction"]),
        liquidity=liquidity_context,
        latest_code=context_code,
        vsa_code=vsa_context_code,
    )
    micro_context = micro_range_context(
        bars,
        atr_values,
        direction="long" if flow_bias > 0 else "short" if flow_bias < 0 else "flat",
        context_ok=abs(flow_bias) >= 4 and not vsa_fuel_latest,
    )
    vix_context = (
        vix_context
        if isinstance(vix_context, dict) and vix_context
        else vix_risk_context(
            bars,
            profile=profile,
            features=features,
            atr_values=atr_values,
            rvol_values=[fact.rvol for fact in facts],
        )
    )
    impulse_confirmed_flags: list[bool] = []
    impulse_aligned_flags: list[bool] = []
    impulse_fuel_flags: list[bool] = []
    for index, item in enumerate(vsa_series):
        item_code = str(item.get("code") or "")
        item_direction = str(item.get("direction") or "flat")
        close_price = bars[index].close if index < len(bars) else latest_bar.close
        item_aligned = (
            item_direction == "long"
            and close_price >= float(ema50[index])
            and close_price >= float(vwap[index])
        ) or (
            item_direction == "short"
            and close_price <= float(ema50[index])
            and close_price <= float(vwap[index])
        )
        impulse_confirmed_flags.append(bool(item_code in VSA_IMPULSE_CODES and bars[index].closed))
        impulse_aligned_flags.append(bool(item_aligned))
        impulse_fuel_flags.append(bool(item.get("fuel")))
    poi_context = impulse_poi_context(
        bars,
        impulse_confirmed=impulse_confirmed_flags,
        impulse_aligned=impulse_aligned_flags,
        impulse_fuel=impulse_fuel_flags,
        vwap_values=vwap,
        runaway_flags=[False for _ in bars],
    )
    reversal_long_valid = bool(
        vwap_lower_2_reversal_valid
        or combined_reversal_long(vsa_code=vsa_context_code, context_code=context_code)
    )
    reversal_short_valid = bool(
        vwap_upper_2_reversal_valid
        or combined_reversal_short(vsa_code=vsa_context_code, context_code=context_code)
    )
    counter_long_unlock = bool(
        reversal_long_valid
        or liquidity_context.get("reclaim_long")
        or (trap_context.get("ready") and trap_context.get("direction") == "long")
    )
    counter_short_unlock = bool(
        reversal_short_valid
        or liquidity_context.get("reclaim_short")
        or (trap_context.get("ready") and trap_context.get("direction") == "short")
    )
    side_gate_context = side_veto_context(
        long_block_lock=bool(contra_long_locked or runaway_day_dn or vwap_lower_2_ride),
        short_block_lock=bool(contra_short_locked or runaway_day_up or vwap_upper_2_ride),
        long_unlock=counter_long_unlock,
        short_unlock=counter_short_unlock,
    )
    entry_gate_context = poi_entry_gate_context(
        poi_active=bool(poi_context.get("active")),
        poi_direction=str(poi_context.get("direction") or "flat"),
        poi_hot=bool(poi_context.get("hot")),
        long_bypass=bool(
            liquidity_context.get("reclaim_long")
            or trap_context.get("direction") == "long"
            or micro_context.get("direction") == "long"
        ),
        short_bypass=bool(
            liquidity_context.get("reclaim_short")
            or trap_context.get("direction") == "short"
            or micro_context.get("direction") == "short"
        ),
        long_warn_trigger=counter_long_unlock,
        short_warn_trigger=bool(counter_short_unlock or vix_context.get("risk_off")),
    )
    poi_long_hot = bool(poi_context.get("hot")) and poi_context.get("direction") == "long"
    poi_short_hot = bool(poi_context.get("hot")) and poi_context.get("direction") == "short"
    seller_rvol_fading = (
        len(facts) >= 3
        and latest_bar.close <= bars[-2].close
        and facts[-1].rvol < facts[-2].rvol <= facts[-3].rvol
    )
    buyer_rvol_fading = (
        len(facts) >= 3
        and latest_bar.close >= bars[-2].close
        and facts[-1].rvol < facts[-2].rvol <= facts[-3].rvol
    )
    opponent_long = bool(
        poi_long_hot and (vsa_context_code in VSA_REVERSAL_LONG_CODES or seller_rvol_fading)
    )
    opponent_short = bool(
        poi_short_hot and (vsa_context_code in VSA_REVERSAL_SHORT_CODES or buyer_rvol_fading)
    )

    side_lock = (
        "LONG"
        if flow_bias >= 2 or runaway_day_up
        else "SHORT"
        if flow_bias <= -2 or runaway_day_dn
        else "CHECK"
    )
    continuation_pattern = _continuation_pattern_with_confirmed_invalidation(
        _continuation_pattern_from_context(indicator_context),
        latest_bar,
    )
    invalidated_pattern_direction = (
        str(continuation_pattern.get("direction") or "flat")
        if continuation_pattern.get("invalidated")
        else "flat"
    )
    if invalidated_pattern_direction == "long" and side_lock == "LONG" and not runaway_day_up:
        side_lock = "CHECK"
    elif invalidated_pattern_direction == "short" and side_lock == "SHORT" and not runaway_day_dn:
        side_lock = "CHECK"
    pullback_reentry = _pullback_reentry_context(
        continuation_pattern=continuation_pattern,
        side_gate_context=side_gate_context,
        latest_ema20=latest_ema20,
        latest_vwap=latest_vwap,
    )
    continuation_active = (
        bool(continuation_pattern.get("active"))
        and str(continuation_pattern.get("phase") or "") != "failed"
    )
    decision_facts = _SpotlightDecisionFacts(
        latest_bar=latest_bar,
        flow_bias=flow_bias,
        flow_conflict=flow_conflict,
        side_lock=side_lock,
        vsa_context_code=vsa_context_code,
        context_code=context_code,
        latest_rvol=latest_rvol,
        fuel_rvol=params.fuel_rvol,
        latest_vwap=latest_vwap,
        latest_ema20=latest_ema20,
        runaway_day_up=runaway_day_up,
        runaway_day_dn=runaway_day_dn,
        vwap_upper_2_ride=vwap_upper_2_ride,
        vwap_lower_2_ride=vwap_lower_2_ride,
        vwap_upper_2_reversal_valid=vwap_upper_2_reversal_valid,
        vwap_lower_2_reversal_valid=vwap_lower_2_reversal_valid,
        reversal_long_valid=reversal_long_valid,
        reversal_short_valid=reversal_short_valid,
        counter_long_unlock=counter_long_unlock,
        counter_short_unlock=counter_short_unlock,
        vsa_fuel_latest=vsa_fuel_latest,
        continuation_active=continuation_active,
        liquidity=liquidity_context,
        trap=trap_context,
        micro=micro_context,
        vix=vix_context,
        poi=poi_context,
        side_gate=side_gate_context,
        entry_gate=entry_gate_context,
        continuation_pattern=continuation_pattern,
        pullback_reentry=pullback_reentry,
    )
    market_state_stage = _market_state_stage(decision_facts)
    entry_status_context = _entry_status_stage(decision_facts)
    risk_context = _risk_stage(decision_facts)

    opponent_direction = (
        "long"
        if opponent_long
        else "short"
        if opponent_short
        else str(poi_context.get("direction") or "flat")
        if poi_context.get("active")
        else "flat"
    )
    opponent_state = (
        "smart_money_buy"
        if opponent_long
        else "smart_money_sell"
        if opponent_short
        else "poi_hot"
        if poi_context.get("hot")
        else "poi_wait"
        if poi_context.get("active")
        else "none"
    )
    opponent_context = {
        "state": opponent_state,
        "direction": opponent_direction,
        "trigger": _round(
            latest_bar.high if opponent_long else latest_bar.low if opponent_short else None
        ),
        "poi_active": bool(poi_context.get("active")),
        "poi_hot": bool(poi_context.get("hot")),
        "exhaustion_long": opponent_long,
        "exhaustion_short": opponent_short,
    }
    invalidation_context = _invalidation_stage(
        decision_facts,
        entry_direction=str(entry_status_context["direction"]),
    )

    strategy_context = strategy_banner_context(
        armed_long=flow_bias >= 2 and side_lock != "SHORT",
        armed_short=flow_bias <= -2 and side_lock != "LONG",
        impulse_confirmed=vsa_context_code in VSA_IMPULSE_CODES
        and latest_bar.closed
        and not vsa_fuel_latest,
        impulse_developing=vsa_context_code in VSA_IMPULSE_CODES
        and not latest_bar.closed
        and not vsa_fuel_latest,
        impulse_direction="long" if flow_bias > 0 else "short" if flow_bias < 0 else "flat",
        impulse_class="CONTEXT",
        impulse_action="wait_pullback",
        price=latest_bar.close,
        flow_bias=flow_bias,
    )
    if continuation_pattern.get("active"):
        strategy_context = {
            **strategy_context,
            "continuation_pattern": continuation_pattern,
            "pullback_reentry": pullback_reentry,
        }
        if continuation_active:
            pattern_direction = str(continuation_pattern.get("direction") or "flat")
            strategy_context.update(
                {
                    "active": True,
                    "mode": "continuation_pattern",
                    "pattern_type": str(continuation_pattern.get("type") or ""),
                    "structure_kind": str(continuation_pattern.get("structure_kind") or ""),
                    "phase": str(continuation_pattern.get("phase") or "watch"),
                    "direction": pattern_direction,
                    "trigger": continuation_pattern.get("break_level"),
                    "invalidation": continuation_pattern.get("invalidation"),
                    "quality": continuation_pattern.get("quality"),
                    "directive": "execute_break"
                    if continuation_pattern.get("phase") == "triggered"
                    else "wait_break_confirmation",
                }
            )
        elif pullback_reentry.get("active"):
            reentry_direction = str(pullback_reentry.get("direction") or "flat")
            strategy_context.update(
                {
                    "active": True,
                    "mode": "pullback_reentry",
                    "phase": str(pullback_reentry.get("phase") or "watch"),
                    "direction": reentry_direction,
                    "trigger": pullback_reentry.get("trigger"),
                    "stop_reference": pullback_reentry.get("stop_reference"),
                    "directive": pullback_reentry.get("directive"),
                }
            )

    long_vote_facts = [
        {
            "code": "vsa_pressure",
            "active": long_pressure > 0,
            "value": long_pressure,
            "sample_size": len(recent_vsa),
        },
        {"code": "price_above_ema20_ema233", "active": trend_up_now},
        {"code": "trend_day", "active": trend_day_bull},
        {"code": "full_trend_alignment", "active": full_bull_now},
        {"code": "ema20_slope", "active": ema20_slope_atr > 0.10, "value": _round(ema20_slope_atr)},
        {
            "code": "vwap_slope",
            "active": vwap_slope_bull and vwap_slope_strength_ok,
            "value": _round(vwap_slope_atr),
        },
        {"code": "m15_score", "active": m15_bull_score >= 4, "value": m15_bull_score},
        {"code": "vwap_pullback_gate", "active": vwap_trend_gate_bull},
        {"code": "runaway_continuation", "active": runaway_day_up or or_no_retest_up},
        {"code": "sigma2_reversal", "active": vwap_lower_2_reversal_valid},
        {
            "code": "vsa_reversal",
            "active": combined_reversal_long(vsa_code=vsa_context_code, context_code=context_code),
        },
        {
            "code": "liquidity_context",
            "active": bool(
                liquidity_context.get("long_context") or liquidity_context.get("reclaim_long")
            ),
        },
        {
            "code": "trap_or_micro",
            "active": bool(
                trap_context.get("direction") == "long" or micro_context.get("direction") == "long"
            ),
        },
        {
            "code": "poi_active",
            "active": bool(poi_context.get("active") and poi_context.get("direction") == "long"),
        },
        {"code": "side_lock", "active": side_lock == "LONG"},
    ]
    short_vote_facts = [
        {
            "code": "vsa_pressure",
            "active": short_pressure > 0,
            "value": short_pressure,
            "sample_size": len(recent_vsa),
        },
        {"code": "price_below_ema20_ema233", "active": trend_dn_now},
        {"code": "trend_day", "active": trend_day_bear},
        {"code": "full_trend_alignment", "active": full_bear_now},
        {
            "code": "ema20_slope",
            "active": ema20_slope_atr < -0.10,
            "value": _round(ema20_slope_atr),
        },
        {
            "code": "vwap_slope",
            "active": vwap_slope_bear and vwap_slope_strength_ok,
            "value": _round(vwap_slope_atr),
        },
        {"code": "m15_score", "active": m15_bear_score >= 4, "value": m15_bear_score},
        {"code": "vwap_pullback_gate", "active": vwap_trend_gate_bear},
        {"code": "runaway_continuation", "active": runaway_day_dn or or_no_retest_down},
        {"code": "sigma2_reversal", "active": vwap_upper_2_reversal_valid},
        {
            "code": "vsa_reversal",
            "active": combined_reversal_short(vsa_code=vsa_context_code, context_code=context_code),
        },
        {
            "code": "liquidity_context",
            "active": bool(
                liquidity_context.get("short_context") or liquidity_context.get("reclaim_short")
            ),
        },
        {
            "code": "trap_or_micro",
            "active": bool(
                trap_context.get("direction") == "short"
                or micro_context.get("direction") == "short"
            ),
        },
        {
            "code": "poi_active",
            "active": bool(poi_context.get("active") and poi_context.get("direction") == "short"),
        },
        {"code": "side_lock", "active": side_lock == "SHORT"},
    ]
    vote_metrics = {
        "long_vote_count": sum(1 for fact in long_vote_facts if fact["active"]),
        "short_vote_count": sum(1 for fact in short_vote_facts if fact["active"]),
        "veto_short": bool(side_gate_context.get("veto_short")),
        "veto_long": bool(side_gate_context.get("veto_long")),
    }

    context_direction = (
        "long" if side_lock == "LONG" else "short" if side_lock == "SHORT" else "flat"
    )
    context_score = pine.clamp(
        50.0 + abs(flow_bias) * 6.0 + (8.0 if trend_day_bull or trend_day_bear else 0.0), 0.0, 96.0
    )
    trend_slowdown = bool(
        vsa_context_code in VSA_ABSORPTION_CODES
        or (side_lock == "LONG" and buyer_rvol_fading)
        or (side_lock == "SHORT" and seller_rvol_fading)
    )
    countertrend_direction = (
        "short" if side_lock == "LONG" else "long" if side_lock == "SHORT" else "flat"
    )
    pullback_allowed = bool(
        (side_lock == "LONG" and counter_short_unlock and not side_gate_context.get("veto_short"))
        or (side_lock == "SHORT" and counter_long_unlock and not side_gate_context.get("veto_long"))
    )
    flow_state = {
        "dominant_direction": context_direction,
        "countertrend_direction": countertrend_direction,
        "chase_risk": bool(
            runaway_day_up
            or runaway_day_dn
            or vwap_upper_2_ride
            or vwap_lower_2_ride
            or vsa_fuel_latest
        ),
        "slowdown": trend_slowdown,
        "pullback_allowed": pullback_allowed,
        "reversal_attempt": {"long": reversal_long_valid, "short": reversal_short_valid},
        "countertrend_unlock": {"long": counter_long_unlock, "short": counter_short_unlock},
        "directive": (
            "avoid_short"
            if side_lock == "LONG" and side_gate_context.get("veto_short")
            else "avoid_long"
            if side_lock == "SHORT" and side_gate_context.get("veto_long")
            else "pullback_watch"
            if pullback_allowed
            else "follow_flow"
            if side_lock in {"LONG", "SHORT"}
            else "wait_balance"
        ),
    }
    market_context = {
        "source": "market_spotlight",
        "version": MARKET_CONTEXT_VERSION,
        "direction": context_direction,
        "score": round(float(context_score), 1),
        "state": market_state_stage.state,
        "state_direction": market_state_stage.direction,
        "attention": market_state_stage.attention,
        "reason_code": market_state_stage.reason_code,
        "flow_bias": flow_bias,
        "flow_state": flow_state,
        "flow_conflict": flow_conflict,
        "side_lock": side_lock,
        "trend_day": {"bull": trend_day_bull, "bear": trend_day_bear},
        "runaway": {"up": runaway_day_up, "down": runaway_day_dn},
        "contra_short_locked": contra_short_locked,
        "contra_long_locked": contra_long_locked,
        "side_gate": side_gate_context,
        "entry": entry_status_context,
        "entry_gate": entry_gate_context,
        "risk": risk_context,
        "opponent": opponent_context,
        "invalidation": invalidation_context,
        "strategy": strategy_context,
        "continuation_pattern": continuation_pattern,
        "pullback_reentry": pullback_reentry,
        "pressure_votes": {"long": long_vote_facts, "short": short_vote_facts},
        "vote_metrics": vote_metrics,
        "closed": bool(latest_bar.closed),
        "metrics": {
            "adx": _round(adx_values[-1]),
            "rvol": _round(latest_rvol),
            "ema20_slope_atr": _round(ema20_slope_atr),
            "vwap_slope_atr": _round(vwap_slope_atr),
            "long_pressure": long_pressure,
            "short_pressure": short_pressure,
        },
        "vsa_context_code": vsa_context_code,
        "context_code": context_code,
        "vwap": {
            "base": _round(latest_vwap),
            "sigma": _round(latest_vwap_sigma),
            "upper_1": _round(latest_upper_1),
            "lower_1": _round(latest_lower_1),
            "upper_2": _round(latest_upper_2),
            "lower_2": _round(latest_lower_2),
            "pullback_long": bool(vwap_trend_gate_bull),
            "pullback_short": bool(vwap_trend_gate_bear),
            "upper_2_ride": bool(vwap_upper_2_ride),
            "lower_2_ride": bool(vwap_lower_2_ride),
        },
        "liquidity": liquidity_context,
        "vix": vix_context,
        "trap": trap_context,
        "micro": micro_context,
        "opening_range": opening_range_context,
        "poi": poi_context,
        "mtf": mtf_context,
    }

    latest_item = {
        "ts": latest_bar.ts.isoformat(),
        "state": market_context["state"],
        "state_direction": market_state_stage.direction,
        "direction": context_direction,
        "flow_bias": flow_bias,
        "flow_state": flow_state,
        "side_lock": side_lock,
        "strategy_phase": str(strategy_context.get("phase") or "off"),
        "score": market_context["score"],
        "rvol": _round(latest_rvol),
        "code": context_code,
        "vsa_context_code": vsa_context_code,
        "market_context": market_context,
        "sentiment_role": "market_context",
        "continuation_pattern": continuation_pattern,
        "pullback_reentry": pullback_reentry,
    }

    table_tip_fields = indicator_fact_payload(
        scenario="market_spotlight",
        trigger_event={"code": market_state_stage.reason_code},
        metrics={
            **market_context["metrics"],
            **vote_metrics,
            "market_state": market_state_stage.state,
            "side_lock": side_lock,
            "pattern_active": bool(continuation_pattern.get("active")),
            "pattern_phase": continuation_pattern.get("phase") or "",
            "pattern_quality": continuation_pattern.get("quality") or "",
            "reentry_active": bool(pullback_reentry.get("active")),
            "reentry_trigger": pullback_reentry.get("trigger"),
        },
    )
    table_style = _normalized_table_style(params.table_style)
    table = {
        "style": table_style,
        "tooltip_mode": "details",
        **table_tip_fields,
        "direction": market_context["direction"],
        "tone": context_direction if context_direction in {"long", "short"} else "warning",
        "market_context": market_context,
        "advisor_visible": True,
        "model_ref": "market_spotlight",
    }

    overlay_items = [
        {
            "type": "table",
            "id": "market_spotlight",
            "source": "market_spotlight",
            "advisor_visible": True,
            "control_key": "table",
            "table": table,
            **table_tip_fields,
        }
    ]

    return {
        "version": MARKET_SPOTLIGHT_VERSION,
        "series": [latest_item],
        "events": [],
        "latest": latest_item,
        "table": table,
        "market_context": market_context,
        "sentiment_role": "market_context",
        "overlays": overlay_items,
    }
