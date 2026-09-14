from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import ActionPhase, Bar
from aef_terminal.indicators.defaults import score_bands_with_thresholds as _score_bands
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    shared_market_series,
    shared_vix_context,
    shared_vsa_facts,
    unavailable_indicator_result,
)
from aef_terminal.features.vsa import VsaFact, vsa_facts
from aef_terminal.features.vsa_classify import (
    VSA_IMPULSE_CODES,
    VSA_REVERSAL_LONG_CODES,
    VSA_REVERSAL_SHORT_CODES,
    bars_since_vsa_code,
    combined_reversal_long,
    combined_reversal_short,
    latest_vsa_context_item,
    vsa_event_code,
    vsa_series_from_facts,
)
from aef_terminal.features.intraday_sessions import session_context_summary
from aef_terminal.features.market_series import MarketSeriesBlock, build_market_series_block
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.features.volume import resolve_rvol_context
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from .action import (
    LINDA_ACTION_CONTRACT_VERSION,
    linda_decision_action as _linda_decision_action,
)
from .analysis import (
    LindaExhaustionInputs,
    LindaGrailInputs,
    analyze_exhaustion,
    analyze_grail,
)
from .plan import (
    plan_for_latest as _plan_for_latest,
    scenario_kind as _scenario_kind,
)
from .overlays import (
    LindaExhaustionOverlayInputs,
    LindaGrailOverlayInputs,
    LindaVwapOverlayInputs,
    build_exhaustion_overlays,
    build_grail_overlays,
    build_poi_overlays,
    build_vwap_overlays,
)
from .params import (
    LindaVolumeParams,
    adapt_linda_bars as _adapt_bars,
    build_params as build_params,
    grail_tuning as _grail_tuning,
    profile_adjusted_params as _profile_adjusted_params,
)
from .playbook_contract import PLAYBOOK_CONTRACT_VERSION, PLAYBOOK_DEFAULT_DECISION_ELIGIBLE
from .series import build_linda_series
from .setups import (
    LindaSetupScanContext,
    playbook_flags_enabled as _playbook_flags_active,
    scan_linda_setups,
    scan_linda_setups_history,
    setup_flags as _setup_flags,
    setup_overlay,
)
from .table import LindaTableInputs, build_linda_table
from aef_terminal.runtime import pine
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.mtf import confirmed_intrabar_parent_preview
from aef_terminal.runtime.signal_state import SignalState, lifecycle_from_signals
from aef_terminal.signals.direction import direction_from_any as _dir_enum
from aef_terminal.features.market_context import (
    impulse_poi_context,
    liquidity_void_context,
    micro_range_context,
    provider_opening_range_context,
    poi_entry_gate_context,
    side_veto_context,
    trap_shift_context,
    true_count,
    vix_risk_context,
)


LINDA_VOLUME_VERSION = "52-python-confirmed-1m-preview"

LINDA_VOLUME_COMPACT_ROW_FIELDS = (
    "code",
    "direction",
    "display_avg",
    "display_volume",
    "ema20",
    "ema233",
    "indian_count",
    "move_atr",
    "pb_stage",
    "range_atr",
    "reason_code",
    "rvol",
    "score",
    "structure_text_ref",
    "structure_text_role",
    "trend_star",
    "ts",
    "vol_rank",
    "vol_z",
    "vwap",
)

LINDA_VOLUME_ROW_FIELDS = (
    "code",
    "direction",
    "display_avg",
    "display_volume",
    "ema20",
    "ema233",
    "important",
    "indian_count",
    "move_atr",
    "pb_stage",
    "range_atr",
    "reason_code",
    "rvol",
    "score",
    "signal",
    "spread_rel",
    "structure_text_ref",
    "structure_text_role",
    "trend_star",
    "ts",
    "vol_rank",
    "vol_z",
    "vwap",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "linda_volume",
        "Linda Theory",
        "Linda Raschke trade-plan hypotheses: Grail, Three Indians and plan management",
        {
            "version": LINDA_VOLUME_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "table": None,
        },
        "lindaVolume",
        "aef_terminal.indicators.modules.linda_volume:linda_volume",
        pipeline_order=40,
        signal_source="linda_volume",
        signal_name_resolver_ref=(
            "aef_terminal.indicators.modules.linda_volume.playbook_contract:resolve_signal_name"
        ),
        candidate_promoter="linda",
        candidate_score_floor_param="min_label_score",
        candidate_score_floor_min=56.0,
        optional_context=("market_spotlight",),
        shared_context_refs=("vsa_facts", "vix_context", "market_series"),
        renderer_primitives=("box", "line", "label", "marker"),
        renderer_placements=("price",),
        renderer_table_label="Linda Theory",
        score_family="reversal",
        empirical_power=1.20,
        usefulness=1.15,
        state_key="lindaVolume",
        calc_key="lindaVolumeCalcEnabled",
        visible_key="lindaVolumeVisible",
        chart_control_id="linda-volume-toggle",
        process_control_id="linda-volume-process",
        api_enabled_key="linda_enabled",
        manager_order=40,
        runtime_order=40,
        table_contract="indicator-table-v1",
        overlay_layer="signals",
        overlay_filter_ref="linda_volume",
        runtime_payload_contract={
            "latest": (
                *LINDA_VOLUME_ROW_FIELDS,
                "lifecycle",
                "rvol_context",
                "table_state",
            ),
            "series": LINDA_VOLUME_ROW_FIELDS,
            "events": LINDA_VOLUME_ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"limit": 240, "fields": "compact_contract"},
            "events": {"limit": 80, "fields": "compact_contract"},
            "overlays": {"limit": 80, "fields": "compact_contract"},
        },
        runtime_payload_compact_contract={
            "series": LINDA_VOLUME_COMPACT_ROW_FIELDS,
            "events": LINDA_VOLUME_COMPACT_ROW_FIELDS,
        },
        default_calc=False,
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe="1m",
                history_bars=64,
                role="lower_timeframe_confirmation",
            ),
        ),
        controls=(
            _control(
                "grail",
                "Grail",
                "toggle",
                True,
                "lindaVolumeGrail",
                element_id="linda-volume-grail",
                api_key="linda_enable_grail",
                param_key="enable_grail",
                action="load",
                compact=True,
            ),
            _control(
                "indians",
                "Indians",
                "toggle",
                True,
                "lindaVolumeIndians",
                element_id="linda-volume-indians",
                api_key="linda_enable_indians",
                param_key="enable_indians",
                action="load",
                compact=True,
            ),
            _control(
                "plan",
                "Plan",
                "toggle",
                True,
                "lindaVolumePlan",
                element_id="linda-volume-plan",
                compact=True,
            ),
            _control(
                "turtleSoup",
                "Turtle Soup",
                "toggle",
                False,
                "lindaVolumeTurtleSoup",
                element_id="linda-volume-turtle-soup",
                api_key="linda_enable_turtle_soup",
                param_key="enable_turtle_soup",
                action="load",
            ),
            _control(
                "turtleSoupPlusOne",
                "+1",
                "toggle",
                False,
                "lindaVolumeTurtleSoupPlusOne",
                element_id="linda-volume-turtle-soup-plus-one",
                api_key="linda_enable_turtle_soup_plus_one",
                param_key="enable_turtle_soup_plus_one",
                action="load",
            ),
            _control(
                "eightyTwenty",
                "80/20",
                "toggle",
                False,
                "lindaVolumeEightyTwenty",
                element_id="linda-volume-eighty-twenty",
                api_key="linda_enable_eighty_twenty",
                param_key="enable_eighty_twenty",
                action="load",
            ),
            _control(
                "theAnti",
                "Anti",
                "toggle",
                False,
                "lindaVolumeTheAnti",
                element_id="linda-volume-the-anti",
                api_key="linda_enable_the_anti",
                param_key="enable_the_anti",
                action="load",
            ),
            _control(
                "momentumPinball",
                "Pinball",
                "toggle",
                False,
                "lindaVolumeMomentumPinball",
                element_id="linda-volume-momentum-pinball",
                api_key="linda_enable_momentum_pinball",
                param_key="enable_momentum_pinball",
                action="load",
            ),
            _control(
                "hvSqueeze",
                "HV",
                "toggle",
                False,
                "lindaVolumeHvSqueeze",
                element_id="linda-volume-hv-squeeze",
                api_key="linda_enable_hv_squeeze",
                param_key="enable_hv_squeeze",
                action="load",
            ),
            _control(
                "adxGapper",
                "ADX",
                "toggle",
                False,
                "lindaVolumeAdxGapper",
                element_id="linda-volume-adx-gapper",
                api_key="linda_enable_adx_gapper",
                param_key="enable_adx_gapper",
                action="load",
            ),
            _control(
                "minScore",
                "Min",
                "number",
                82,
                "lindaVolumeMinScore",
                element_id="linda-volume-min-score",
                api_key="linda_min_score",
                param_key="min_score",
                minimum=45,
                maximum=95,
                step=1,
                action="load",
            ),
            _control(
                "indianMinScore",
                "Indian",
                "number",
                72,
                "lindaVolumeIndianMinScore",
                element_id="linda-volume-indian-min-score",
                api_key="linda_indian_min_score",
                param_key="indian_min_score",
                minimum=45,
                maximum=95,
                step=1,
                action="load",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.linda_volume:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.linda_volume:build_params",
    candidate_promoter_ref=(
        "aef_terminal.indicators.modules.linda_volume.candidates:"
        "signal_candidates_from_linda_indicator"
    ),
    ui_js_assets=("client.js",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = linda_volume
    params = ctx.params("linda_volume")
    confirmed_micro_preview_bars = confirmed_intrabar_parent_preview(
        ctx.confirmed_bars,
        ctx.confirmed_bar_context.get("1m", ()),
        ctx.confirmed_bar_context_quality.get("1m"),
        "1m",
    )
    preview_features = {
        "display_symbol": str(
            ctx.features.get("display_symbol")
            or (ctx.analysis_latest.symbol if ctx.analysis_latest is not None else "")
        )
    }
    return IndicatorExecutionSpec(
        id="linda_volume",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate_with_context=lambda dependency_context: calculate(
            ctx.confirmed_bars,
            profile=ctx.instrument_profile,
            params=params,
            features=ctx.features,
            facts=shared_vsa_facts(ctx),
            vix_context=shared_vix_context(ctx),
            market_series=shared_market_series(ctx),
            market_session=dependency_context.get("market_spotlight"),
            vwap_session=ctx.vwap_session,
        ),
        preview_calculate_with_context=(
            (
                lambda dependency_context: calculate(
                    confirmed_micro_preview_bars,
                    profile=ctx.instrument_profile,
                    params=params,
                    features=preview_features,
                    facts=None,
                    vix_context=shared_vix_context(ctx),
                    market_series=None,
                    market_session=(
                        dependency_context.get("market_spotlight", {}).get("preview")
                        if isinstance(
                            dependency_context.get("market_spotlight"),
                            dict,
                        )
                        else None
                    ),
                    vwap_session=ctx.vwap_session,
                    preview_only=True,
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
    )


def _session_market_context(market_session: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(market_session, dict):
        return {}
    market_context = market_session.get("market_context")
    return market_context if isinstance(market_context, dict) else {}


def _session_context_block(session_market_context: dict[str, Any], key: str) -> dict[str, Any]:
    block = session_market_context.get(key)
    return block if isinstance(block, dict) else {}


def linda_volume(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    params: LindaVolumeParams | None = None,
    features: dict[str, Any] | None = None,
    facts: Sequence[VsaFact] | None = None,
    market_session: dict[str, Any] | None = None,
    vix_context: dict[str, Any] | None = None,
    market_series: MarketSeriesBlock | None = None,
    vwap_session: ProviderSessionReset | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    params = params or LindaVolumeParams()
    features = features or {}
    if not bars:
        return {
            "version": LINDA_VOLUME_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "table": None,
            "playbook_setups": {
                "experimental": True,
                "decision_eligible": PLAYBOOK_DEFAULT_DECISION_ELIGIBLE,
                "active": False,
                "items": [],
            },
        }
    if vwap_session is None or not vwap_session.available:
        return unavailable_indicator_result(INDICATOR_MODULE.spec.empty_result, vwap_session)
    params, profile_label = _profile_adjusted_params(profile, params)

    facts = list(facts) if facts is not None else vsa_facts(bars, params, vwap_session=vwap_session)
    if market_series is None:
        market_series = build_market_series_block(
            list(bars),
            ema_pullback_len=params.ema_pullback_len,
            ema_trend_len=params.ema_trend_len,
            ema_magnet_len=params.ema_magnet_len,
            atr_len=params.atr_len,
            vwap_session=vwap_session,
        )
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    ema20 = market_series.ema_pullback
    ema50 = market_series.ema_trend
    ema233 = market_series.ema_magnet
    vwap = market_series.vwap
    vwap_sigma, vwap_upper_1, vwap_lower_1, vwap_upper_2, vwap_lower_2 = (
        market_series.vwap_sigma,
        market_series.vwap_upper_1,
        market_series.vwap_lower_1,
        market_series.vwap_upper_2,
        market_series.vwap_lower_2,
    )
    atr_values = market_series.atr_sma
    volume_avg = pine.sma_series(volumes, 20)
    adx_values = market_series.adx
    mtf_context = market_series.mtf_context
    indian_spacing_bars = _adapt_bars(params.indian_spacing_bars, bars[-1].timeframe)
    indian_extension_lookback = max(
        2, _adapt_bars(params.indian_extension_lookback, bars[-1].timeframe)
    )

    series_result = build_linda_series(
        bars=bars,
        facts=facts,
        highs=highs,
        lows=lows,
        ema20=ema20,
        ema233=ema233,
        vwap=vwap,
        atr_values=atr_values,
        volume_avg=volume_avg,
        adx_values=adx_values,
        params=params,
        score_bands=_score_bands(
            watch=params.score_watch,
            arm=params.score_arm,
            go=params.score_go,
        ),
        indian_spacing_bars=indian_spacing_bars,
        indian_extension_lookback=indian_extension_lookback,
        preview_only=preview_only,
    )
    series = series_result.series
    events = series_result.events
    signal_states = series_result.signals
    indian_overlay_items = series_result.overlay_items
    last_signal = series_result.last_signal

    latest_item = dict(series[-1])
    latest_dir = latest_item.get("direction") or "flat"
    session_market_context = _session_market_context(market_session)
    session_flow_bias = int(session_market_context.get("flow_bias") or 0)
    session_contra_short_locked = bool(session_market_context.get("contra_short_locked", False))
    session_contra_long_locked = bool(session_market_context.get("contra_long_locked", False))
    session_trend_day = (
        session_market_context.get("trend_day")
        if isinstance(session_market_context.get("trend_day"), dict)
        else {}
    )
    session_trend_day_bull = bool(session_trend_day.get("bull", False))
    session_trend_day_bear = bool(session_trend_day.get("bear", False))
    session_runaway = (
        session_market_context.get("runaway")
        if isinstance(session_market_context.get("runaway"), dict)
        else {}
    )
    session_runaway_day_up = bool(session_runaway.get("up", False))
    session_runaway_day_dn = bool(session_runaway.get("down", False))
    session_liquidity_context = _session_context_block(session_market_context, "liquidity")
    session_vix_context = _session_context_block(session_market_context, "vix")
    vsa_series = vsa_series_from_facts(facts, importance_floor=float(params.min_label_score))
    vsa_latest = vsa_series[-1] if vsa_series else {}
    vsa_ctx = latest_vsa_context_item(vsa_series)
    vsa_context_code = vsa_event_code(vsa_ctx)
    vsa_fuel_latest = bool(vsa_latest.get("fuel"))
    vsa_climax_latest = bool(vsa_latest.get("terminal_climax"))

    recent_events = [event for event in events[-12:] if event.get("important")]
    long_pressure = sum(1 for event in recent_events if event.get("direction") == "long")
    short_pressure = sum(1 for event in recent_events if event.get("direction") == "short")
    latest_bar = bars[-1]
    latest_atr = max(atr_values[-1], 0.000001)
    latest_ema20 = ema20[-1]
    latest_ema50 = ema50[-1]
    latest_ema233 = ema233[-1]
    latest_vwap = vwap[-1]
    latest_vwap_sigma = vwap_sigma[-1]
    latest_upper_1 = vwap_upper_1[-1]
    latest_lower_1 = vwap_lower_1[-1]
    latest_upper_2 = vwap_upper_2[-1]
    latest_lower_2 = vwap_lower_2[-1]
    ema20_base = ema20[-6] if len(ema20) >= 6 else ema20[0]
    ema233_base = ema233[-12] if len(ema233) >= 12 else ema233[0]
    vwap_base_3 = vwap[-4] if len(vwap) >= 4 else vwap[0]
    ema20_slope_atr = (latest_ema20 - ema20_base) / latest_atr
    ema233_slope_atr = (latest_ema233 - ema233_base) / latest_atr
    vwap_slope_atr = (latest_vwap - vwap_base_3) / latest_atr
    vwap_slope_bull = vwap_slope_atr > 0
    vwap_slope_bear = vwap_slope_atr < 0
    vwap_slope_strength_ok = abs(vwap_slope_atr) >= 0.03
    trend_up_now = latest_bar.close > latest_ema20 >= latest_ema233 and ema20_slope_atr >= -0.02
    trend_dn_now = latest_bar.close < latest_ema20 <= latest_ema233 and ema20_slope_atr <= 0.02
    full_bull_now = latest_bar.close > latest_ema50 and latest_bar.close > latest_vwap
    full_bear_now = latest_bar.close < latest_ema50 and latest_bar.close < latest_vwap
    mtf_bull_ok = bool(mtf_context["filter_bull_ok"])
    mtf_bear_ok = bool(mtf_context["filter_bear_ok"])
    m15_bull_score = int(mtf_context["bull_score"])
    m15_bear_score = int(mtf_context["bear_score"])
    vwap_trend_confirm_bull = adx_values[-1] > params.indian_adx_min or m15_bull_score >= 4
    vwap_trend_confirm_bear = adx_values[-1] > params.indian_adx_min or m15_bear_score >= 4
    vwap_trend_gate_bull = vwap_slope_bull and vwap_slope_strength_ok and vwap_trend_confirm_bull
    vwap_trend_gate_bear = vwap_slope_bear and vwap_slope_strength_ok and vwap_trend_confirm_bear
    trend_day_bull = (
        session_trend_day_bull
        if session_market_context
        else full_bull_now and mtf_bull_ok and vwap_slope_bull
    )
    trend_day_bear = (
        session_trend_day_bear
        if session_market_context
        else full_bear_now and mtf_bear_ok and vwap_slope_bear
    )
    context_item = latest_item if latest_item.get("code") else last_signal or latest_item
    context_code = str(context_item.get("code") or "")
    context_dir = str(context_item.get("direction") or latest_dir)
    flow_bias = session_flow_bias if session_market_context else long_pressure - short_pressure
    if not session_market_context:
        flow_bias += 2 if trend_up_now else -2 if trend_dn_now else 0
        flow_bias += 1 if ema20_slope_atr > 0.10 else -1 if ema20_slope_atr < -0.10 else 0
        flow_bias = int(pine.clamp(flow_bias, -5, 5))
    flow_conflict = bool(long_pressure > 0 and short_pressure > 0 and abs(flow_bias) <= 1)
    flow = "LONG" if flow_bias >= 2 else "SHORT" if flow_bias <= -2 else "BAL"
    vwap_band_ready = latest_vwap_sigma > 0.000001
    vwap_touch_upper_2 = [
        vwap_band_ready and bar.high >= up for bar, up in zip(bars, vwap_upper_2, strict=False)
    ]
    vwap_touch_lower_2 = [
        vwap_band_ready and bar.low <= dn for bar, dn in zip(bars, vwap_lower_2, strict=False)
    ]
    vwap_close_upper_1 = [bar.close > up for bar, up in zip(bars, vwap_upper_1, strict=False)]
    vwap_close_lower_1 = [bar.close < dn for bar, dn in zip(bars, vwap_lower_1, strict=False)]
    vwap_upper_2_ride = (
        true_count(vwap_touch_upper_2, 8) >= 3
        and latest_bar.close > latest_upper_2
        and vwap_slope_bull
        and (full_bull_now or m15_bull_score >= 4 or vsa_context_code == "FUEL_UP")
    )
    vwap_lower_2_ride = (
        true_count(vwap_touch_lower_2, 8) >= 3
        and latest_bar.close < latest_lower_2
        and vwap_slope_bear
        and (full_bear_now or m15_bear_score >= 4 or vsa_context_code == "FUEL_DN")
    )
    vwap_upper_1_ride = (
        true_count(vwap_close_upper_1, 8) >= 5
        and latest_bar.close > latest_upper_1
        and vwap_slope_bull
        and full_bull_now
    )
    vwap_lower_1_ride = (
        true_count(vwap_close_lower_1, 8) >= 5
        and latest_bar.close < latest_lower_1
        and vwap_slope_bear
        and full_bear_now
    )
    vwap_touch_tol = max(latest_atr * 0.05, 0.000001)
    latest_anatomy = pine.bar_anatomy(latest_bar)
    directional_close_ok = (
        latest_anatomy.close_pos >= 0.62
        if latest_bar.close >= latest_bar.open
        else latest_anatomy.close_pos <= 0.38
    )
    latest_rvol = float(latest_item.get("rvol") or 0.0)
    canonical_rvol, rvol_source = resolve_rvol_context(features)
    if canonical_rvol is None:
        canonical_rvol = latest_rvol
        rvol_source = "linda_volume_row" if latest_rvol > 0.0 else "unavailable"
    latest_item["rvol_context"] = {
        "bar_rvol": _round(latest_rvol),
        "canonical_rvol": _round(canonical_rvol),
        "source": rvol_source,
    }
    liquidity_context = (
        session_liquidity_context
        if session_market_context
        else liquidity_void_context(
            bars,
            atr_values,
            [fact.rvol for fact in facts],
            min_rvol=max(0.95, params.pullback_rvol),
        )
    )
    vix_context = (
        session_vix_context
        if session_market_context and session_vix_context
        else vix_context
        if isinstance(vix_context, dict) and vix_context
        else vix_risk_context(
            bars,
            profile=profile,
            features=features,
            atr_values=atr_values,
            rvol_values=[fact.rvol for fact in facts],
        )
    )
    vwap_pullback_long_active = (
        vwap_trend_gate_bull
        and (trend_day_bull or full_bull_now)
        and bool(liquidity_context.get("long_context"))
        and latest_bar.low <= latest_vwap + vwap_touch_tol
        and latest_bar.close > latest_vwap
        and latest_bar.close > latest_bar.open
        and latest_rvol >= max(params.pullback_rvol, 0.95)
        and directional_close_ok
        and not bool(vsa_fuel_latest)
    )
    vwap_pullback_short_active = (
        vwap_trend_gate_bear
        and (trend_day_bear or full_bear_now)
        and bool(liquidity_context.get("short_context"))
        and latest_bar.high >= latest_vwap - vwap_touch_tol
        and latest_bar.close < latest_vwap
        and latest_bar.close < latest_bar.open
        and latest_rvol >= max(params.pullback_rvol, 0.95)
        and directional_close_ok
        and not bool(vsa_fuel_latest)
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
    vix_put_extreme = vwap_band_ready and (
        vwap_touch_upper_2[-1]
        or latest_bar.close >= latest_vwap + latest_vwap_sigma * 2.5
        or latest_anatomy.upper_share >= 0.42
    )
    vix_put_local = vwap_upper_2_reversal_valid or combined_reversal_short(
        vsa_code=vsa_context_code, context_code=context_code
    )
    vix_put_watch = bool(vix_context.get("risk_off")) and (
        bool(vix_context.get("shock")) or vix_put_extreme
    )
    vix_put_setup = vix_put_watch and vix_put_extreme and vix_put_local and latest_bar.closed
    runaway_day_up = (
        session_runaway_day_up
        if session_market_context
        else trend_day_bull
        and flow_bias >= 3
        and (vwap_upper_1_ride or latest_bar.close > latest_upper_1)
        and not vwap_upper_2_reversal_valid
    )
    runaway_day_dn = (
        session_runaway_day_dn
        if session_market_context
        else trend_day_bear
        and flow_bias <= -3
        and (vwap_lower_1_ride or latest_bar.close < latest_lower_1)
        and not vwap_lower_2_reversal_valid
    )
    latest_state = str(latest_item.get("code") or "wait").lower()
    last_indian = next((event for event in reversed(events) if event.get("indian_count")), None)
    stretch_up = (
        latest_bar.close > latest_ema20 + latest_atr * 1.35
        or latest_bar.close > latest_ema233 + latest_atr * 3.8
    )
    stretch_dn = (
        latest_bar.close < latest_ema20 - latest_atr * 1.35
        or latest_bar.close < latest_ema233 - latest_atr * 3.8
    )
    contra_short_locked = (
        session_contra_short_locked
        if session_market_context
        else (
            (trend_up_now or trend_day_bull or vwap_upper_2_ride or runaway_day_up)
            and (ema20_slope_atr > 0.08 or stretch_up or vwap_upper_2_ride or runaway_day_up)
        )
        and vsa_context_code not in VSA_REVERSAL_SHORT_CODES
    )
    contra_long_locked = (
        session_contra_long_locked
        if session_market_context
        else (
            (trend_dn_now or trend_day_bear or vwap_lower_2_ride or runaway_day_dn)
            and (ema20_slope_atr < -0.08 or stretch_dn or vwap_lower_2_ride or runaway_day_dn)
        )
        and vsa_context_code not in VSA_REVERSAL_LONG_CODES
    )
    liq_relong_active = (
        bool(liquidity_context.get("long_context"))
        and (trend_day_bull or trend_up_now)
        and latest_bar.close > latest_vwap
        and latest_bar.close >= latest_bar.open
        and latest_rvol >= max(0.95, params.pullback_rvol)
    )
    liq_reshort_active = (
        bool(liquidity_context.get("short_context"))
        and (trend_day_bear or trend_dn_now)
        and latest_bar.close < latest_vwap
        and latest_bar.close < latest_bar.open
        and latest_rvol >= max(0.95, params.pullback_rvol)
    )
    liq_reclaim_long_active = bool(liquidity_context.get("reclaim_long"))
    liq_reclaim_short_active = bool(liquidity_context.get("reclaim_short"))
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
    trap_ready = bool(trap_context.get("ready"))
    trap_execution = bool(trap_context.get("execution"))
    trap_direction = str(trap_context.get("direction") or "flat")
    opening_range_context = provider_opening_range_context(
        bars,
        provider_session=vwap_session,
    )
    impulse_confirmed_series = [vsa_event_code(item) in VSA_IMPULSE_CODES for item in vsa_series]
    impulse_aligned_series = [
        (
            str(vsa_series[index].get("direction") or "") == "long"
            and bars[index].close > ema20[index]
            and bars[index].close > vwap[index]
            and ema20[index] >= ema233[index]
        )
        or (
            str(vsa_series[index].get("direction") or "") == "short"
            and bars[index].close < ema20[index]
            and bars[index].close < vwap[index]
            and ema20[index] <= ema233[index]
        )
        for index in range(len(bars))
    ]
    impulse_fuel_series = [bool(item.get("fuel")) for item in vsa_series]
    runaway_series = [
        (
            str(vsa_series[index].get("direction") or "") == "long"
            and bars[index].close > vwap_upper_1[index]
            and ema20[index] >= ema233[index]
        )
        or (
            str(vsa_series[index].get("direction") or "") == "short"
            and bars[index].close < vwap_lower_1[index]
            and ema20[index] <= ema233[index]
        )
        for index in range(len(bars))
    ]
    poi_context = impulse_poi_context(
        bars,
        impulse_confirmed=impulse_confirmed_series,
        impulse_aligned=impulse_aligned_series,
        impulse_fuel=impulse_fuel_series,
        vwap_values=vwap,
        runaway_flags=runaway_series,
        expire_bars=30,
    )
    if (
        bool(opening_range_context.get("no_retest_up"))
        and trend_day_bull
        and not vwap_upper_2_reversal_valid
    ):
        runaway_day_up = True
    if (
        bool(opening_range_context.get("no_retest_down"))
        and trend_day_bear
        and not vwap_lower_2_reversal_valid
    ):
        runaway_day_dn = True
    grail = _grail_tuning(profile)
    grail_fresh_bars = _adapt_bars(8, latest_bar.timeframe)

    latest_indian_count = int(latest_item.get("indian_count") or 0)
    bars_since_impulse_up = bars_since_vsa_code(vsa_series, "FUEL_UP")
    bars_since_impulse_down = bars_since_vsa_code(vsa_series, "FUEL_DN")
    bars_since_pb_up = bars_since_vsa_code(series, "PB_UP")
    bars_since_pb_dn = bars_since_vsa_code(series, "PB_DN")
    or_break_up_fresh = (
        opening_range_context.get("bars_since_break_up") is not None
        and int(opening_range_context.get("bars_since_break_up") or 0) <= grail_fresh_bars
    )
    or_break_down_fresh = (
        opening_range_context.get("bars_since_break_down") is not None
        and int(opening_range_context.get("bars_since_break_down") or 0) <= grail_fresh_bars
    )
    fresh_long_initiative = (
        (bars_since_impulse_up is not None and bars_since_impulse_up <= grail_fresh_bars)
        or or_break_up_fresh
        or (bars_since_pb_up is not None and bars_since_pb_up <= grail_fresh_bars)
        or (latest_indian_count >= 1 and str(latest_item.get("code") or "") == "PB_UP")
    )
    fresh_short_initiative = (
        (bars_since_impulse_down is not None and bars_since_impulse_down <= grail_fresh_bars)
        or or_break_down_fresh
        or (bars_since_pb_dn is not None and bars_since_pb_dn <= grail_fresh_bars)
        or (latest_indian_count >= 1 and str(latest_item.get("code") or "") == "PB_DN")
    )
    recent_long_flip = any(
        (bars_since := bars_since_vsa_code(vsa_series, code)) is not None and bars_since <= 12
        for code in ("SPRING", "EXH_UP")
    ) or any(
        (bars_since := bars_since_vsa_code(series, code)) is not None and bars_since <= 12
        for code in ("NO_SUPPLY", "VW_RECLAIM")
    )
    recent_short_flip = any(
        (bars_since := bars_since_vsa_code(vsa_series, code)) is not None and bars_since <= 12
        for code in ("UPTHRUST", "EXH_DN")
    ) or any(
        (bars_since := bars_since_vsa_code(series, code)) is not None and bars_since <= 12
        for code in ("NO_DEMAND", "VW_REJECT")
    )
    trend_long_unlock = bool(
        recent_long_flip
        and (
            liq_reclaim_long_active
            or vwap_lower_2_reversal_valid
            or (latest_bar.close > latest_ema20 and latest_bar.close > latest_vwap)
        )
    )
    trend_short_unlock = bool(
        recent_short_flip
        and (
            liq_reclaim_short_active
            or vwap_upper_2_reversal_valid
            or (latest_bar.close < latest_ema20 and latest_bar.close < latest_vwap)
        )
    )
    long_contra_allowed = bool(
        trend_long_unlock
        or liq_reclaim_long_active
        or vwap_lower_2_reversal_valid
        or (trap_ready and trap_direction == "long")
    )
    short_contra_allowed = bool(
        trend_short_unlock
        or liq_reclaim_short_active
        or vwap_upper_2_reversal_valid
        or vix_put_setup
        or (trap_ready and trap_direction == "short")
    )
    side_gate_context = side_veto_context(
        long_block_lock=trend_dn_now
        or trend_day_bear
        or runaway_day_dn
        or vwap_lower_2_ride
        or contra_long_locked,
        short_block_lock=trend_up_now
        or trend_day_bull
        or runaway_day_up
        or vwap_upper_2_ride
        or contra_short_locked,
        long_unlock=trend_long_unlock,
        short_unlock=trend_short_unlock,
        long_contra_allowed=long_contra_allowed,
        short_contra_allowed=short_contra_allowed,
    )
    side_veto_long = bool(side_gate_context.get("veto_long"))
    side_veto_short = bool(side_gate_context.get("veto_short"))
    grail_analysis = analyze_grail(
        LindaGrailInputs(
            latest_bar=latest_bar,
            highs=highs,
            lows=lows,
            latest_atr=latest_atr,
            latest_ema20=latest_ema20,
            latest_ema233=latest_ema233,
            latest_adx=adx_values[-1],
            latest_volume_avg=volume_avg[-1],
            params=params,
            tuning=grail,
            full_bull=full_bull_now,
            full_bear=full_bear_now,
            mtf_bull_ok=mtf_bull_ok,
            mtf_bear_ok=mtf_bear_ok,
            vsa_fuel_latest=bool(vsa_fuel_latest),
            fresh_long_initiative=fresh_long_initiative,
            fresh_short_initiative=fresh_short_initiative,
            latest_indian_count=latest_indian_count,
            m15_bull_score=m15_bull_score,
            m15_bear_score=m15_bear_score,
            contra_long_locked=contra_long_locked,
            contra_short_locked=contra_short_locked,
            side_veto_long=side_veto_long,
            side_veto_short=side_veto_short,
        )
    )
    too_far = grail_analysis.too_far
    grail_long_stop = grail_analysis.long_stop
    grail_short_stop = grail_analysis.short_stop
    grail_long_target = grail_analysis.long_target
    grail_short_target = grail_analysis.short_target
    grail_buy_rr = grail_analysis.buy_rr
    grail_sell_rr = grail_analysis.sell_rr
    grail_buy_developing = grail_analysis.buy_developing
    grail_sell_developing = grail_analysis.sell_developing
    is_grail_buy = grail_analysis.is_buy
    is_grail_sell = grail_analysis.is_sell
    grail_buy_blockers = grail_analysis.buy_blockers
    grail_sell_blockers = grail_analysis.sell_blockers
    exhaustion_analysis = analyze_exhaustion(
        LindaExhaustionInputs(
            bars=bars,
            series=series,
            vsa_series=vsa_series,
            latest_rvol=latest_rvol,
            anatomy_upper_share=latest_anatomy.upper_share,
            anatomy_lower_share=latest_anatomy.lower_share,
            anatomy_close_pos=latest_anatomy.close_pos,
            poi_context=poi_context,
            context_code=context_code,
            vsa_context_code=vsa_context_code,
            params=params,
            vsa_fuel_latest=bool(vsa_fuel_latest),
            too_far=too_far,
            vwap_touch_upper_2=vwap_touch_upper_2,
            vwap_touch_lower_2=vwap_touch_lower_2,
            latest_upper_1=latest_upper_1,
            latest_lower_1=latest_lower_1,
            latest_upper_2=latest_upper_2,
            latest_lower_2=latest_lower_2,
            vwap_upper_2_reversal_valid=vwap_upper_2_reversal_valid,
            vwap_lower_2_reversal_valid=vwap_lower_2_reversal_valid,
            trend_day_bull=trend_day_bull,
            trend_day_bear=trend_day_bear,
            full_bull=full_bull_now,
            full_bear=full_bear_now,
            runaway_day_up=runaway_day_up,
            runaway_day_dn=runaway_day_dn,
            latest_atr=latest_atr,
        )
    )
    prev_rvol = exhaustion_analysis.prev_rvol
    prev2_rvol = exhaustion_analysis.prev2_rvol
    poi_long_hot = exhaustion_analysis.poi_long_hot
    poi_short_hot = exhaustion_analysis.poi_short_hot
    opp_exh_long = exhaustion_analysis.opp_exh_long
    opp_exh_short = exhaustion_analysis.opp_exh_short
    upper_volume_exhaustion = exhaustion_analysis.upper_volume_exhaustion
    upper_location_exhaustion = exhaustion_analysis.upper_location_exhaustion
    lower_location_exhaustion = exhaustion_analysis.lower_location_exhaustion
    bull_exhaust_score = exhaustion_analysis.bull_exhaust_score
    bear_exhaust_score = exhaustion_analysis.bear_exhaust_score
    bull_exhaust_candidate = exhaustion_analysis.bull_exhaust_candidate
    bear_exhaust_candidate = exhaustion_analysis.bear_exhaust_candidate
    clx_ct_long = exhaustion_analysis.clx_ct_long
    clx_ct_short = exhaustion_analysis.clx_ct_short
    rev_warning = exhaustion_analysis.reversal_warning
    impulse_direction = "long" if latest_bar.close >= latest_bar.open else "short"
    impulse_clean = vsa_context_code in VSA_IMPULSE_CODES and not vsa_fuel_latest
    impulse_confirmed = bool(impulse_clean and latest_bar.closed)
    impulse_developing = bool(impulse_clean and not latest_bar.closed)
    impulse_bias_aligned = (impulse_direction == "long" and full_bull_now and mtf_bull_ok) or (
        impulse_direction == "short" and full_bear_now and mtf_bear_ok
    )
    impulse_active = bool(impulse_confirmed or impulse_developing)
    indicator_alerts: list[dict[str, Any]] = []
    micro_exhaust_watch = bool(
        vsa_fuel_latest
        or bool(vsa_ctx.get("fuel"))
        or bull_exhaust_candidate
        or bear_exhaust_candidate
        or rev_warning
        or too_far
    )
    micro_long_ctx = bool(
        flow_bias >= 4
        and full_bull_now
        and latest_bar.close > latest_ema20
        and latest_bar.close > latest_vwap
        and (
            trend_day_bull
            or runaway_day_up
            or vwap_upper_1_ride
            or (impulse_active and impulse_direction == "long")
        )
        and not (vwap_pullback_long_active or vwap_pullback_short_active)
        and not micro_exhaust_watch
        and not vwap_upper_2_reversal_valid
    )
    micro_short_ctx = bool(
        flow_bias <= -4
        and full_bear_now
        and latest_bar.close < latest_ema20
        and latest_bar.close < latest_vwap
        and (
            trend_day_bear
            or runaway_day_dn
            or vwap_lower_1_ride
            or (impulse_active and impulse_direction == "short")
        )
        and not (vwap_pullback_long_active or vwap_pullback_short_active)
        and not micro_exhaust_watch
        and not vwap_lower_2_reversal_valid
    )
    micro_long_context = micro_range_context(
        bars,
        atr_values,
        direction="long",
        context_ok=micro_long_ctx,
        length=5,
        tight_atr=0.90,
    )
    micro_short_context = micro_range_context(
        bars,
        atr_values,
        direction="short",
        context_ok=micro_short_ctx,
        length=5,
        tight_atr=0.90,
    )
    micro_context = micro_long_context if micro_long_context.get("active") else micro_short_context
    micro_long_break = bool(micro_long_context.get("break"))
    micro_short_break = bool(micro_short_context.get("break"))
    micro_long_watch = bool(micro_long_context.get("watch"))
    micro_short_watch = bool(micro_short_context.get("watch"))
    poi_gate_context = poi_entry_gate_context(
        poi_active=bool(poi_context.get("active")),
        poi_direction=str(poi_context.get("direction") or "flat"),
        poi_hot=bool(poi_context.get("hot")),
        long_bypass=bool(
            poi_long_hot
            or clx_ct_long
            or liq_reclaim_long_active
            or liq_relong_active
            or (trap_ready and trap_direction == "long")
            or micro_long_watch
            or micro_long_break
        ),
        short_bypass=bool(
            poi_short_hot
            or clx_ct_short
            or liq_reclaim_short_active
            or liq_reshort_active
            or (trap_ready and trap_direction == "short")
            or micro_short_watch
            or micro_short_break
        ),
        long_confirm_trigger=bool(
            latest_bar.closed
            and not side_veto_long
            and (
                is_grail_buy
                or vwap_pullback_long_active
                or clx_ct_long
                or liq_relong_active
                or liq_reclaim_long_active
                or ((trap_ready and trap_direction == "long") and trap_execution)
            )
        ),
        short_confirm_trigger=bool(
            latest_bar.closed
            and not side_veto_short
            and (
                is_grail_sell
                or vwap_pullback_short_active
                or clx_ct_short
                or liq_reshort_active
                or liq_reclaim_short_active
                or ((trap_ready and trap_direction == "short") and trap_execution)
            )
        ),
        long_warn_trigger=bool(
            not side_veto_long
            and (
                grail_buy_developing
                or clx_ct_long
                or micro_long_watch
                or micro_long_break
                or (impulse_active and impulse_direction == "long" and impulse_bias_aligned)
                or (trap_ready and trap_direction == "long")
            )
        ),
        short_warn_trigger=bool(
            not side_veto_short
            and (
                grail_sell_developing
                or clx_ct_short
                or micro_short_watch
                or micro_short_break
                or (impulse_active and impulse_direction == "short" and impulse_bias_aligned)
                or (trap_ready and trap_direction == "short")
            )
        ),
    )
    poi_gate_active = bool(poi_gate_context.get("active"))
    poi_gate_long = bool((poi_gate_context.get("long") or {}).get("allowed"))
    poi_gate_short = bool((poi_gate_context.get("short") or {}).get("allowed"))
    entry_long_confirm = bool((poi_gate_context.get("long") or {}).get("confirm"))
    entry_short_confirm = bool((poi_gate_context.get("short") or {}).get("confirm"))
    entry_long_warn = bool((poi_gate_context.get("long") or {}).get("warn"))
    entry_short_warn = bool((poi_gate_context.get("short") or {}).get("warn"))
    plan = _plan_for_latest(latest_item, latest_bar, latest_atr, flow)
    plan_dir = _dir_enum(str(plan.get("direction") or "flat"))
    plan_kind = _scenario_kind(str(latest_item.get("code") or ""))
    plan_code = str(latest_item.get("code") or "")
    plan_score = float(latest_item["score"])
    plan_action = _linda_decision_action(
        code=plan_code,
        score=plan_score,
        min_label_score=params.min_label_score,
        bands=_score_bands(
            watch=params.score_watch,
            arm=params.score_arm,
            go=params.score_go,
        ),
        blocked=bool(plan.get("blocked")),
    )
    plan_signal = SignalState(
        source="linda_volume",
        action=ActionPhase(plan_action),
        raw_action=str(plan.get("action") or plan_action),
        direction=plan_dir,
        score=plan_score,
        confirmed=latest_bar.closed,
        source_tf=latest_bar.timeframe,
        trigger=float(plan["trigger"]) if plan["trigger"] is not None else None,
        stop=float(plan["stop"]) if plan["stop"] is not None else None,
        target=float(plan["target"]) if plan["target"] is not None else None,
        invalidation=float(plan["stop"]) if plan["stop"] is not None else None,
        reason=str(plan.get("reason_code") or latest_item.get("reason_code") or "linda_wait"),
        blocked_reason=str(plan.get("blocked_reason") or ""),
        code=plan_code,
        kind=plan_kind,
    )
    plan_signal_payload = plan_signal.as_dict()
    plan_signal_payload["reason_code"] = plan_signal_payload.pop(
        "reason",
        str(plan.get("reason_code") or latest_item.get("reason_code") or "linda_wait"),
    )
    latest_item["signal"] = dict(plan_signal_payload)
    lifecycle = lifecycle_from_signals(
        source="linda_volume",
        bars=bars,
        signals=signal_states,
        atr_values=atr_values,
        max_age_bars=18,
    )
    latest_item["lifecycle"] = lifecycle.as_dict() if lifecycle else None
    entry_conflict = bool(entry_long_confirm and entry_short_confirm)
    ct_watch_long = bool(
        side_veto_long
        and (
            clx_ct_long
            or liq_reclaim_long_active
            or liq_relong_active
            or (trap_ready and trap_direction == "long")
            or combined_reversal_long(vsa_code=vsa_context_code, context_code=context_code)
            or vwap_lower_2_reversal_valid
        )
    )
    ct_watch_short = bool(
        side_veto_short
        and (
            clx_ct_short
            or liq_reclaim_short_active
            or liq_reshort_active
            or vix_put_setup
            or (trap_ready and trap_direction == "short")
            or combined_reversal_short(vsa_code=vsa_context_code, context_code=context_code)
            or vwap_upper_2_reversal_valid
        )
    )
    poi_gate_context["conflict"] = entry_conflict
    poi_gate_context["ct_watch"] = {
        "long": ct_watch_long,
        "short": ct_watch_short,
    }
    table_result = build_linda_table(
        LindaTableInputs(
            latest_state=latest_state,
            plan_signal=plan_signal,
            plan_dir=plan_dir,
            plan=plan,
            plan_code=plan_code,
            latest_item=latest_item,
            last_indian=last_indian,
            entry_conflict=entry_conflict,
            ct_watch_long=ct_watch_long,
            ct_watch_short=ct_watch_short,
            flow_conflict=flow_conflict,
            side_veto_long=side_veto_long,
            side_veto_short=side_veto_short,
            poi_gate_active=poi_gate_active,
            poi_gate_long=poi_gate_long,
            poi_gate_short=poi_gate_short,
            entry_long_confirm=entry_long_confirm,
            entry_short_confirm=entry_short_confirm,
            entry_long_warn=entry_long_warn,
            entry_short_warn=entry_short_warn,
            micro_long_break=micro_long_break,
            micro_short_break=micro_short_break,
            micro_long_watch=micro_long_watch,
            micro_short_watch=micro_short_watch,
            micro_context=micro_context,
            is_grail_buy=is_grail_buy,
            is_grail_sell=is_grail_sell,
            grail_buy_rr=grail_buy_rr,
            grail_sell_rr=grail_sell_rr,
            liq_relong_active=liq_relong_active,
            liq_reshort_active=liq_reshort_active,
            liq_reclaim_long_active=liq_reclaim_long_active,
            liq_reclaim_short_active=liq_reclaim_short_active,
            trap_ready=trap_ready,
            trap_execution=trap_execution,
            trap_direction=trap_direction,
            trap_context=trap_context,
            vsa_fuel_latest=vsa_fuel_latest,
            vsa_climax_latest=vsa_climax_latest,
            vsa_context_code=vsa_context_code,
            context_code=context_code,
            context_dir=context_dir,
            runaway_day_up=runaway_day_up,
            runaway_day_dn=runaway_day_dn,
            vwap_upper_2_ride=vwap_upper_2_ride,
            vwap_lower_2_ride=vwap_lower_2_ride,
            contra_short_locked=contra_short_locked,
            contra_long_locked=contra_long_locked,
            vsa_ctx=vsa_ctx,
            vsa_latest=vsa_latest,
            vix_put_watch=vix_put_watch,
            vix_context=vix_context,
            liquidity_context=liquidity_context,
            flow_bias=flow_bias,
            opp_exh_long=opp_exh_long,
            opp_exh_short=opp_exh_short,
            poi_context=poi_context,
            latest_bar=latest_bar,
            latest_vwap=latest_vwap,
            ema20_slope_atr=ema20_slope_atr,
            ema233_slope_atr=ema233_slope_atr,
        )
    )
    table_state = table_result.state
    latest_item["table_state"] = table_state
    table = table_result.table
    overlay_items: list[dict[str, Any]] = [] if preview_only else [*indian_overlay_items]
    if not preview_only:
        overlay_items.extend(
            build_poi_overlays(
                bars=bars,
                latest_bar=latest_bar,
                poi_context=poi_context,
            )
        )
        overlay_items.extend(
            build_grail_overlays(
                LindaGrailOverlayInputs(
                    latest_bar=latest_bar,
                    latest_item=latest_item,
                    params=params,
                    tuning=grail,
                    latest_ema20=latest_ema20,
                    is_buy=is_grail_buy,
                    is_sell=is_grail_sell,
                    buy_developing=grail_buy_developing,
                    sell_developing=grail_sell_developing,
                    buy_stop=grail_long_stop,
                    buy_target=grail_long_target,
                    buy_rr=grail_buy_rr,
                    sell_stop=grail_short_stop,
                    sell_target=grail_short_target,
                    sell_rr=grail_sell_rr,
                    buy_blockers=grail_buy_blockers,
                    sell_blockers=grail_sell_blockers,
                )
            )
        )
        overlay_items.extend(
            build_exhaustion_overlays(
                LindaExhaustionOverlayInputs(
                    latest_bar=latest_bar,
                    poi_context=poi_context,
                    latest_ema20=latest_ema20,
                    latest_vwap=latest_vwap,
                    latest_rvol=latest_rvol,
                    prev_rvol=prev_rvol,
                    prev2_rvol=prev2_rvol,
                    opp_exh_long=opp_exh_long,
                    opp_exh_short=opp_exh_short,
                    clx_ct_long=clx_ct_long,
                    clx_ct_short=clx_ct_short,
                    bull_candidate=bull_exhaust_candidate,
                    bear_candidate=bear_exhaust_candidate,
                    bull_score=bull_exhaust_score,
                    bear_score=bear_exhaust_score,
                    upper_volume_exhaustion=upper_volume_exhaustion,
                    upper_location_exhaustion=upper_location_exhaustion,
                    lower_location_exhaustion=lower_location_exhaustion,
                    reversal_warning=rev_warning,
                    trend_day_bull=trend_day_bull,
                    full_bull_now=full_bull_now,
                )
            )
        )
        overlay_items.extend(
            build_vwap_overlays(
                LindaVwapOverlayInputs(
                    latest_bar=latest_bar,
                    params=params,
                    latest_vwap=latest_vwap,
                    latest_rvol=latest_rvol,
                    latest_upper_2=latest_upper_2,
                    latest_lower_2=latest_lower_2,
                    pullback_long_active=vwap_pullback_long_active,
                    pullback_short_active=vwap_pullback_short_active,
                    upper_2_reversal_valid=vwap_upper_2_reversal_valid,
                    upper_2_ride=vwap_upper_2_ride,
                    lower_2_reversal_valid=vwap_lower_2_reversal_valid,
                    lower_2_ride=vwap_lower_2_ride,
                    touch_upper_2=vwap_touch_upper_2,
                    touch_lower_2=vwap_touch_lower_2,
                )
            )
        )
    playbook_setups: dict[str, Any] = {
        "experimental": True,
        "decision_eligible": PLAYBOOK_DEFAULT_DECISION_ELIGIBLE,
        "contract": PLAYBOOK_CONTRACT_VERSION,
        "symbol": latest_bar.symbol,
        "display_symbol": str((features or {}).get("display_symbol") or latest_bar.symbol),
        "session": session_context_summary(
            bars,
            provider_session=vwap_session,
        ),
        "active": False,
        "items": [],
    }
    if not preview_only and _playbook_flags_active(params):
        setup_flags = _setup_flags(params)
        playbook_lookback = max(_adapt_bars(64, latest_bar.timeframe), 24)

        def _setup_context_at(index: int) -> LindaSetupScanContext:
            return LindaSetupScanContext(
                atr=max(float(atr_values[index]), 0.000001),
                adx=float(adx_values[index] if adx_values else 0.0),
                ema20=float(ema20[index]),
                ema50=float(ema50[index]),
                volume_avg=float(volume_avg[index] if volume_avg else 0.0),
                provider_session=vwap_session,
                score_bands=_score_bands(
                    watch=params.score_watch,
                    arm=params.score_arm,
                    go=params.score_go,
                ),
            )

        setup_signals = scan_linda_setups(
            bars,
            setup_flags,
            context=_setup_context_at(len(bars) - 1),
        )
        history_signals = scan_linda_setups_history(
            bars,
            setup_flags,
            context_at=_setup_context_at,
            lookback=playbook_lookback,
        )
        playbook_setups["active"] = bool(setup_signals or history_signals)
        playbook_setups["items"] = [item.to_dict() for item in setup_signals]
        playbook_setups["history_count"] = len(history_signals)
        for bar_index, setup_signal in history_signals:
            overlay_items.append(setup_overlay(setup_signal, bars[bar_index]))
    return {
        "version": LINDA_VOLUME_VERSION,
        "series": series,
        "events": (
            [event for event in events if event.get("ts") == latest_bar.ts.isoformat()]
            if preview_only
            else events
        ),
        "latest": latest_item,
        "plan": plan,
        "table": table,
        "playbook_setups": playbook_setups,
        "overlays": overlay_items,
        "alerts": indicator_alerts,
        "params": {
            "profile": profile_label,
            "use_profile_thresholds": params.use_profile_thresholds,
            "impulse_rvol": params.impulse_rvol,
            "fuel_rvol": params.fuel_rvol,
            "fuel_range_atr": params.fuel_range_atr,
            "min_label_score": params.min_label_score,
            "pullback_rvol": params.pullback_rvol,
            "indian_adx_min": params.indian_adx_min,
            "indian_volume_mult": params.indian_volume_mult,
            "enable_grail": params.enable_grail,
            "enable_indians": params.enable_indians,
            "enable_turtle_soup": params.enable_turtle_soup,
            "enable_turtle_soup_plus_one": params.enable_turtle_soup_plus_one,
            "enable_eighty_twenty": params.enable_eighty_twenty,
            "enable_the_anti": params.enable_the_anti,
            "enable_momentum_pinball": params.enable_momentum_pinball,
            "enable_hv_squeeze": params.enable_hv_squeeze,
            "enable_adx_gapper": params.enable_adx_gapper,
            "action_contract": LINDA_ACTION_CONTRACT_VERSION,
        },
    }
