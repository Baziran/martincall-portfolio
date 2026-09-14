from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import (
    current_futures_contract_id,
    instrument_is_futures,
    instrument_provider,
    provider_contract_id,
    provider_symbol,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
)
from aef_terminal.engine.gex_context import build_gex_dynamics_context
from aef_terminal.engine.vsa_context import (
    align_vsa_volume_series,
    empty_vsa_volume_context,
)
from aef_terminal.engine.decision import DecisionContext, choose_decision
from aef_terminal.features.chart_guides import build_chart_guides, display_feature_context
from aef_terminal.indicators.registry import (
    indicator_ids,
    indicator_ids_for_output_contract,
)
from aef_terminal.indicators.scoring import indicator_score_overrides
from aef_terminal.runtime.presentation import (
    build_decision_action_card,
)
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.signal_state import active_lifecycle_from_indicators
from aef_terminal.signals.quality import (
    apply_global_min_rr_gate,
    apply_global_min_rr_to_decision,
)
from aef_terminal.engine.common import round_to_tick
from aef_terminal.engine.data_quality import (
    snapshot_freshness,
    data_quality_report,
    serialize_data_quality_report,
)
from aef_terminal.engine.sentiment import (
    _evaluate_institutional_edge,
    _institutional_edge_overlay,
    build_direction_sentiment,
)
from aef_terminal.engine.signal_lifecycle import (
    active_signal_candidates,
    annotate_signal_candidate_lifecycles,
    expire_indicator_lifecycles,
)
from aef_terminal.engine.serialization import (
    serialize_bars,
    serialize_candidate,
    serialize_decision,
    serialize_features,
)
from aef_terminal.engine.trade_setup import (
    TRADE_SETUP_AUTHORITY_CONTRACT,
    blocked_trade_setup_execution_authority,
    build_blocked_trade_setup_card,
    build_trade_setup_card,
    trade_setup_execution_authority,
)

from aef_terminal.engine.analyze.features import _json_safe
from aef_terminal.engine.analyze.indicator_status import (
    attach_indicator_status,
    empty_indicator_result,
)
from aef_terminal.engine.analyze.inputs import AnalyzeInputBundle, build_analyze_inputs
from aef_terminal.engine.analyze.pipeline import (
    _calculate_post_decision_indicators,
    _calculate_primary_signal_indicators,
    _calculate_structure_signal_indicators,
    _calculate_setup_stage_indicators,
)


def _decision_authoritative_candidates(
    candidates: Sequence[SignalCandidate],
) -> list[SignalCandidate]:
    return [candidate for candidate in candidates if candidate.decision_authoritative]


def _candidate_is_preview(candidate: SignalCandidate) -> bool:
    return candidate.finality is CandidateFinality.PROVISIONAL


def _annotate_execution_candidates(candidates: Sequence[SignalCandidate]) -> list[SignalCandidate]:
    out: list[SignalCandidate] = []
    for candidate in candidates:
        execution_candidate = candidate.decision_authoritative
        details = dict(candidate.details or {})
        details["execution_candidate"] = execution_candidate
        out.append(replace(candidate, details=details))
    return out


def _build_preview_meta(
    *,
    inputs: AnalyzeInputBundle,
    latest: Bar,
    candidates: Sequence[SignalCandidate],
    execution_candidates: Sequence[SignalCandidate],
) -> dict[str, Any]:
    provisional_bar = latest.closed is False
    preview_candidate_count = sum(1 for candidate in candidates if _candidate_is_preview(candidate))
    return {
        "active": bool(provisional_bar or preview_candidate_count),
        "bar_closed": bool(latest.closed),
        "provisional_bar": bool(provisional_bar),
        "candidate_count": int(preview_candidate_count),
        "execution_candidate_count": len(execution_candidates),
        "confirmed_bar_count": len(inputs.confirmed_bars),
        "live_signal_bar_count": len(inputs.live_signal_bars),
        "latest_ts": latest.ts.isoformat(),
        "confirmed_latest_ts": inputs.analysis_latest.ts.isoformat(),
        "source": latest.source,
        "policy": "preview_only_until_exchange_confirmed",
        "execution_uses_confirmed": True,
    }


def _finalize_analysis_snapshot(
    snapshot: dict[str, Any],
    *,
    serialize_output: bool,
) -> dict[str, Any]:
    if not serialize_output:
        return snapshot
    return _json_safe(snapshot)


def _no_confirmed_bar_snapshot(
    *,
    inputs: AnalyzeInputBundle,
    latest: Bar,
    display_symbol: str | None,
    bar_slots: ProviderBarSlotSequence | None,
    analysis_as_of_utc: datetime,
    data_delay_grace_seconds: float,
    data_quality_store: Any | None,
    instrument: dict[str, Any] | None,
    data_provider: str | None,
    provider_symbol_value: str,
    gex_context_active: bool,
    gex_capture_mode: GexCaptureMode,
) -> dict[str, Any]:
    warning = "No exchange-confirmed OHLCV bar is available for signal execution"
    quality = data_quality_report(
        inputs.bars,
        latest.timeframe,
        now_utc=analysis_as_of_utc,
        tail_delay_grace_seconds=data_delay_grace_seconds,
        store=data_quality_store,
        instrument=instrument,
        instrument_profile=inputs.instrument_profile,
    )
    quality.update(
        {
            "signals_ok": False,
            "status": "no_confirmed_bar",
            "warning": warning,
        }
    )
    decision = ScenarioDecision(
        kind=ScenarioKind.WAIT,
        direction=Direction.FLAT,
        confidence=0.0,
        action=ActionPhase.BLOCK,
        trigger=None,
        stop=None,
        target=None,
        invalidation=None,
        reasons=[],
        source="data_quality",
        trigger_event=DomainFact(
            "data_quality_block",
            {"quality_status": "no_confirmed_bar"},
        ),
        reason_codes=["data_quality_block"],
        metrics={"data_quality_status": "no_confirmed_bar"},
    )
    execution_authority = blocked_trade_setup_execution_authority(
        "no_confirmed_bar",
        provider_ids=indicator_ids_for_output_contract(TRADE_SETUP_AUTHORITY_CONTRACT),
    )
    trade_setup = build_blocked_trade_setup_card(
        execution_authority=execution_authority,
        reason_code="no_confirmed_bar",
        watch_event="wait_for_exchange_confirmed_bar",
        signal_source=decision.source,
    )
    command_card = dict(trade_setup["action_card"])
    updated_at = datetime.now(tz=UTC)
    preview_meta = _build_preview_meta(
        inputs=inputs,
        latest=latest,
        candidates=[],
        execution_candidates=[],
    )
    preview_meta["active"] = True
    preview_meta["blocked_reason"] = "no_confirmed_bar"
    empty_indicators = {
        indicator_id: attach_indicator_status(
            indicator_id,
            empty_indicator_result(indicator_id),
            input_bars=[],
            analysis_bar=None,
            mode="confirmed",
            error=ValueError(warning),
        )
        for indicator_id in indicator_ids()
    }
    snapshot = {
        "meta": {
            "symbol": display_symbol or latest.symbol,
            "provider_symbol": provider_symbol_value,
            "instrument_id": qualified_instrument_id(instrument),
            "route_fingerprint": route_fingerprint(instrument),
            "provider": instrument_provider(instrument),
            "provider_contract_id": current_futures_contract_id(instrument)
            if instrument_is_futures(instrument)
            else provider_contract_id(instrument),
            "timeframe": latest.timeframe,
            "source": latest.source,
            "preview": preview_meta,
            "price": round(latest.close, 4),
            "change": 0.0,
            "change_pct": 0.0,
            "live_bar_closed": bool(latest.closed),
            "analysis_ts": None,
            "analysis_as_of_utc": inputs.analysis_as_of_utc.isoformat(),
            "confirmed_bar_count": 0,
            "security_context": {},
            "option_flow": None,
            "tick_bias": None,
            "instrument_profile": inputs.instrument_profile.as_meta(),
            "price_increment": inputs.price_increment,
            "data_quality": serialize_data_quality_report(quality),
            "warning": "",
            "updated_at": updated_at.isoformat(),
            "freshness": snapshot_freshness(
                timeframe=latest.timeframe,
                source=str(latest.source or "unknown"),
                latest_bar_ts=latest.ts,
                updated_at=updated_at,
                instrument_profile=inputs.instrument_profile,
            ),
        },
        "bars": serialize_bars(
            inputs.bars,
            bar_slots,
            session_reset=inputs.vwap_session,
        ),
        "chart_guides": [],
        "features": serialize_features({}, 0.0),
        "candidates": [],
        "decision": serialize_decision(
            decision,
            price_increment=inputs.price_increment,
        ),
        "command": command_card,
        "direction_sentiment": {
            "direction": Direction.FLAT.value,
            "score": 0,
            "contributors": [],
            "summary": warning,
        },
        "trade_setup": trade_setup,
        "lifecycle": None,
        "indicators": empty_indicators,
        "vsa_volume": empty_vsa_volume_context("no_confirmed_bars"),
        "levels": [],
    }
    if gex_context_active:
        snapshot["gex_dynamics"] = build_gex_dynamics_context(
            [],
            (),
            capture_mode=gex_capture_mode,
        )
    return snapshot


def analyze_bars(
    bars: Sequence[Bar],
    display_symbol: str | None = None,
    indicator_params: dict[str, Any] | None = None,
    mtf_context: dict[str, Sequence[Bar]] | None = None,
    mtf_context_slots: Mapping[str, ProviderBarSlotSequence] | None = None,
    mtf_quality: Mapping[str, Mapping[str, Any]] | None = None,
    bar_slots: ProviderBarSlotSequence | None = None,
    gex_history: Sequence[dict[str, Any]] | None = None,
    option_targets: Sequence[dict[str, Any]] | None = None,
    option_flow: dict[str, Any] | None = None,
    tick_flow: dict[str, Any] | None = None,
    analysis_as_of_utc: datetime | None = None,
    data_delay_grace_seconds: float = 0.0,
    include_telemetry: bool = False,
    inputs: AnalyzeInputBundle | None = None,
    data_quality_store: Any | None = None,
    instrument: dict[str, Any] | None = None,
    data_provider: str | None = None,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
    serialize_output: bool = True,
) -> dict[str, Any]:
    if not bars and inputs is None:
        raise ValueError("At least one bar is required")
    if type(gex_context_active) is not bool:
        raise TypeError("GEX context active state must be a boolean")
    provider_symbol_value = provider_symbol(instrument, instrument_provider(instrument))
    if inputs is None:
        inputs = build_analyze_inputs(
            bars,
            indicator_params=indicator_params,
            mtf_context=mtf_context,
            mtf_context_slots=mtf_context_slots,
            mtf_quality=mtf_quality,
            tick_flow=tick_flow,
            bar_slots=bar_slots,
            analysis_as_of_utc=analysis_as_of_utc,
            instrument=instrument,
        )
    elif analysis_as_of_utc is not None:
        if (
            not isinstance(analysis_as_of_utc, datetime)
            or analysis_as_of_utc.tzinfo is None
            or analysis_as_of_utc.utcoffset() is None
        ):
            raise ValueError("analysis_as_of_utc must be timezone-aware")
        if analysis_as_of_utc.astimezone(UTC) != inputs.analysis_as_of_utc:
            raise ValueError("analysis_as_of_utc disagrees with AnalyzeInputBundle")
    latest = inputs.latest
    if not inputs.confirmed_bars:
        return _finalize_analysis_snapshot(
            _no_confirmed_bar_snapshot(
                inputs=inputs,
                latest=latest,
                display_symbol=display_symbol,
                bar_slots=bar_slots,
                analysis_as_of_utc=inputs.analysis_as_of_utc,
                data_delay_grace_seconds=data_delay_grace_seconds,
                data_quality_store=data_quality_store,
                instrument=instrument,
                data_provider=data_provider,
                provider_symbol_value=provider_symbol_value,
                gex_context_active=gex_context_active,
                gex_capture_mode=gex_capture_mode,
            ),
            serialize_output=serialize_output,
        )
    instrument_profile = inputs.instrument_profile
    quality = data_quality_report(
        inputs.bars,
        latest.timeframe,
        now_utc=inputs.analysis_as_of_utc,
        tail_delay_grace_seconds=data_delay_grace_seconds,
        store=data_quality_store,
        instrument=instrument,
        instrument_profile=instrument_profile,
    )
    previous = inputs.bars[-2] if len(inputs.bars) >= 2 else latest
    change = latest.close - previous.close
    change_pct = change / previous.close * 100.0 if previous.close else 0.0
    features = dict(inputs.features)
    features["display_symbol"] = display_symbol or latest.symbol
    score_overrides = indicator_score_overrides(inputs.indicator_params)
    indicator_timings: dict[str, float] | None = {} if include_telemetry else None
    candidates: list[SignalCandidate] = list(inputs.cached_context.primitives.candidates)

    (
        primary_indicators,
        primary_candidates,
        vsa_context,
        vsa_fuel_sfp_cooldown,
        primary_shared_context,
    ) = _calculate_primary_signal_indicators(
        confirmed_bars=inputs.confirmed_bars,
        live_signal_bars=inputs.live_signal_bars,
        latest=latest,
        analysis_latest=inputs.analysis_latest,
        runtime_params=inputs.runtime_params,
        global_defaults=inputs.global_defaults,
        indicator_params=inputs.indicator_params,
        instrument_profile=instrument_profile,
        instrument_id=inputs.instrument_id,
        route_fingerprint=inputs.route_fingerprint,
        provider=inputs.provider,
        analysis_as_of_utc=inputs.analysis_as_of_utc,
        quality=quality,
        vwap_session=inputs.vwap_session,
        features=features,
        feature_context=inputs.feature_context,
        option_flow=option_flow,
        tick_flow=inputs.tick_flow,
        manual_channel_drawings=inputs.manual_channel_drawings,
        confirmed_slots=inputs.confirmed_slots,
        confirmed_bar_context=inputs.confirmed_bar_context,
        confirmed_bar_context_slots=inputs.confirmed_bar_context_slots,
        confirmed_bar_context_quality=inputs.confirmed_bar_context_quality,
        atr_value=inputs.atr_value,
        strong_watch_floor=inputs.strong_watch_floor,
        structure_floor=inputs.structure_floor,
        score_overrides=score_overrides,
        timings=indicator_timings,
    )
    candidates.extend(primary_candidates)
    if vsa_fuel_sfp_cooldown:
        features["vsa_fuel_sfp_cooldown"] = vsa_fuel_sfp_cooldown
    structure_indicators, structure_candidates = _calculate_structure_signal_indicators(
        inputs=inputs,
        indicator_bundle=primary_indicators,
        shared=primary_shared_context,
        score_overrides=score_overrides,
        timings=indicator_timings,
    )
    candidates.extend(structure_candidates)
    pre_decision_bundle = {**primary_indicators, **structure_indicators}
    setup_indicators, setup_candidates = _calculate_setup_stage_indicators(
        confirmed_bars=inputs.confirmed_bars,
        analysis_latest=inputs.analysis_latest,
        analysis_as_of_utc=inputs.analysis_as_of_utc,
        runtime_params=inputs.runtime_params,
        indicator_params=inputs.indicator_params,
        features=features,
        indicator_bundle=pre_decision_bundle,
        shared=primary_shared_context,
        confirmed_bar_context=inputs.confirmed_bar_context,
        confirmed_bar_context_slots=inputs.confirmed_bar_context_slots,
        confirmed_bar_context_quality=inputs.confirmed_bar_context_quality,
        instrument_profile=instrument_profile,
        structure_floor=inputs.structure_floor,
        atr_value=inputs.atr_value,
        score_overrides=score_overrides,
        timings=indicator_timings,
    )
    execution_authority = trade_setup_execution_authority(
        indicator_ids_for_output_contract(TRADE_SETUP_AUTHORITY_CONTRACT),
        enabled=inputs.runtime_params.enabled,
        indicator_results=setup_indicators,
    )
    candidates.extend(setup_candidates)
    candidates = annotate_signal_candidate_lifecycles(candidates, inputs.confirmed_bars)
    active_candidates = active_signal_candidates(candidates)
    signal_quality_params = (
        inputs.indicator_params.get("signal_quality")
        if isinstance(inputs.indicator_params.get("signal_quality"), dict)
        else {}
    )
    signal_min_rr = (
        signal_quality_params.get("min_rr") if isinstance(signal_quality_params, dict) else None
    )
    execution_candidates = _decision_authoritative_candidates(active_candidates)
    decision_candidates, _, rr_gate = apply_global_min_rr_gate(
        execution_candidates,
        signal_min_rr,
    )
    _, display_candidates, _ = apply_global_min_rr_gate(active_candidates, signal_min_rr)
    candidates = _annotate_execution_candidates(display_candidates)
    preview_meta = _build_preview_meta(
        inputs=inputs,
        latest=latest,
        candidates=candidates,
        execution_candidates=decision_candidates,
    )
    features["signal_quality"] = rr_gate
    decision = choose_decision(
        DecisionContext(
            candidates=tuple(decision_candidates),
            bars=tuple(inputs.confirmed_bars),
            atr_value=inputs.atr_value,
            strategy_mode=inputs.runtime_params.strategy_mode,
            option_flow=option_flow,
            instrument_profile=instrument_profile,
            price_increment=inputs.price_increment,
        )
    )
    decision = apply_global_min_rr_to_decision(decision, signal_min_rr)
    if not quality.get("signals_ok", True):
        quality_status = str(quality.get("status") or "")
        decision = ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.BLOCK,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[str(quality.get("warning") or "Market data is inconsistent")],
            source="data_quality",
            trigger_event=DomainFact(
                "data_quality_block",
                {"quality_status": quality_status or "unknown"},
            ),
            reason_codes=["data_quality_block"],
        )
    indicator_bundle = {**pre_decision_bundle, **setup_indicators}
    institutional_edge = _evaluate_institutional_edge(
        indicator_bundle,
        vsa_context=vsa_context,
    )
    institutional_edge_overlay = (
        _institutional_edge_overlay(institutional_edge, inputs.analysis_latest)
        if institutional_edge
        else None
    )
    institutional_edge_source = (
        str(institutional_edge.get("source") or "") if isinstance(institutional_edge, dict) else ""
    )
    institutional_edge_owner = indicator_bundle.get(institutional_edge_source)
    if institutional_edge_overlay is not None and isinstance(institutional_edge_owner, dict):
        institutional_edge_owner.setdefault("overlays", []).append(institutional_edge_overlay)
    expire_indicator_lifecycles(indicator_bundle, inputs.confirmed_bars)
    active_lifecycle = active_lifecycle_from_indicators(indicator_bundle)
    post_decision_indicators = _calculate_post_decision_indicators(
        confirmed_bars=inputs.confirmed_bars,
        analysis_latest=inputs.analysis_latest,
        analysis_as_of_utc=inputs.analysis_as_of_utc,
        decision=decision,
        decision_candidates=decision_candidates,
        features=features,
        indicator_bundle=indicator_bundle,
        instrument_profile=instrument_profile,
        quality=quality,
        vsa_context=vsa_context,
        runtime_params=inputs.runtime_params,
        indicator_params=inputs.indicator_params,
        option_targets=option_targets,
        confirmed_bar_context=inputs.confirmed_bar_context,
        confirmed_bar_context_slots=inputs.confirmed_bar_context_slots,
        confirmed_bar_context_quality=inputs.confirmed_bar_context_quality,
        atr_value=inputs.atr_value,
        score_overrides=score_overrides,
        timings=indicator_timings,
    )
    show_visuals = bool(inputs.indicator_params.get("show_visuals", True))
    if not show_visuals:
        for ind in [*indicator_bundle.values(), *post_decision_indicators.values()]:
            if isinstance(ind, dict) and "overlays" in ind:
                ind["overlays"] = []
        vsa_context = {**vsa_context, "overlays": []}

    lookback = (
        inputs.confirmed_bars[-30:] if len(inputs.confirmed_bars) >= 30 else inputs.confirmed_bars
    )
    local_high = max(bar.high for bar in lookback)
    local_low = min(bar.low for bar in lookback)
    serialized_decision = serialize_decision(
        decision,
        price_increment=inputs.price_increment,
    )
    serialized_decision["lifecycle"] = active_lifecycle
    serialized_bars = serialize_bars(
        inputs.bars,
        bar_slots,
        session_reset=inputs.vwap_session,
    )
    chart_feature = display_feature_context(
        inputs.feature_context,
        live_feature_context=inputs.live_feature_context,
        display_bars=inputs.bars,
    )
    chart_guides = build_chart_guides(
        inputs.bars,
        symbol=display_symbol or latest.symbol,
        feature_context=chart_feature,
        instrument=instrument,
        display_axis=serialized_bars,
        context_bars=inputs.bars,
        include_telemetry=include_telemetry,
    )
    core_levels = [
        {
            "name": "local high",
            "price": (
                round_to_tick(local_high, inputs.price_increment)
                if inputs.price_increment is not None
                else float(local_high)
            ),
            "kind": "resistance",
            "role": "local_high",
        },
        {
            "name": "local low",
            "price": (
                round_to_tick(local_low, inputs.price_increment)
                if inputs.price_increment is not None
                else float(local_low)
            ),
            "kind": "support",
            "role": "local_low",
        },
    ]
    chart_vwap = chart_guides.get("latest", {}).get("vwap")
    if isinstance(chart_vwap, (int, float)):
        core_levels.append(
            {
                "name": "session vwap",
                "price": (
                    round_to_tick(float(chart_vwap), inputs.price_increment)
                    if inputs.price_increment is not None
                    else float(chart_vwap)
                ),
                "kind": "fair",
                "role": "session_vwap",
            }
        )
    trade_setup_indicators = dict(indicator_bundle)
    trade_setup = build_trade_setup_card(
        latest=inputs.analysis_latest,
        decision=decision,
        candidates=decision_candidates,
        indicators=trade_setup_indicators,
        levels=core_levels,
        option_flow=option_flow,
        data_quality=quality,
        atr_value=inputs.atr_value,
        price_increment=inputs.price_increment,
        execution_authority=execution_authority,
    )
    command_card = trade_setup.get("action_card")
    if not isinstance(command_card, dict):
        command_card = build_decision_action_card(decision, data_quality=quality).as_dict()
    direction_sentiment = build_direction_sentiment(
        decision=decision,
        indicators=trade_setup_indicators,
        vsa_context=vsa_context,
        candidates=decision_candidates,
        tick_bias=inputs.tick_bias,
        instrument_profile=instrument_profile,
    )
    snapshot_indicator_bundle = {**indicator_bundle, **post_decision_indicators}
    aligned_vsa = dict(vsa_context)
    aligned_vsa["series"] = align_vsa_volume_series(
        (vsa_context.get("series") if isinstance(vsa_context.get("series"), list) else []),
        serialized_bars,
    )
    status = aligned_vsa.get("status")
    if isinstance(status, dict):
        aligned_vsa["status"] = {
            **status,
            "series_count": len(aligned_vsa["series"]),
        }

    updated_at = datetime.now(tz=UTC)
    snapshot = {
        "meta": {
            "symbol": display_symbol or latest.symbol,
            "provider_symbol": provider_symbol_value,
            "instrument_id": qualified_instrument_id(instrument),
            "route_fingerprint": route_fingerprint(instrument),
            "provider": instrument_provider(instrument),
            "provider_contract_id": current_futures_contract_id(instrument)
            if instrument_is_futures(instrument)
            else provider_contract_id(instrument),
            "timeframe": latest.timeframe,
            "source": latest.source,
            "preview": preview_meta,
            "price": round(latest.close, 4),
            "change": round(change, 4),
            "change_pct": round(change_pct, 4),
            "live_bar_closed": bool(latest.closed),
            "analysis_ts": inputs.analysis_latest.ts.isoformat(),
            "analysis_as_of_utc": inputs.analysis_as_of_utc.isoformat(),
            "confirmed_bar_count": len(inputs.confirmed_bars),
            "security_context": {
                timeframe: {
                    "bar_count": len(context_bars),
                    "first_ts": context_bars[0].ts.isoformat() if context_bars else None,
                    "last_ts": context_bars[-1].ts.isoformat() if context_bars else None,
                    "source": context_bars[-1].source if context_bars else None,
                    "quality": {
                        key: value
                        for key, value in (
                            inputs.security_context_quality.get(
                                timeframe,
                                {},
                            )
                        ).items()
                        if key != "eligible_ranges"
                    },
                }
                for timeframe, context_bars in inputs.security_context.items()
            },
            "confirmed_bar_context": {
                timeframe: {
                    "bar_count": len(context_bars),
                    "first_ts": (context_bars[0].ts.isoformat() if context_bars else None),
                    "last_ts": (context_bars[-1].ts.isoformat() if context_bars else None),
                    "source": (context_bars[-1].source if context_bars else None),
                    "quality": {
                        key: value
                        for key, value in inputs.confirmed_bar_context_quality.get(
                            timeframe,
                            {},
                        ).items()
                        if key != "eligible_ranges"
                    },
                }
                for timeframe, context_bars in inputs.confirmed_bar_context.items()
            },
            "option_flow": option_flow,
            "tick_bias": inputs.tick_bias,
            "instrument_profile": instrument_profile.as_meta(),
            "price_increment": inputs.price_increment,
            "data_quality": serialize_data_quality_report(quality),
            "warning": "",
            "updated_at": updated_at.isoformat(),
            "freshness": snapshot_freshness(
                timeframe=latest.timeframe,
                source=str(latest.source or "unknown"),
                latest_bar_ts=latest.ts,
                updated_at=updated_at,
                instrument_profile=instrument_profile,
            ),
        },
        "bars": serialized_bars,
        "chart_guides": chart_guides,
        "features": serialize_features(features, inputs.atr_value),
        "candidates": [
            serialize_candidate(
                candidate,
                price_increment=inputs.price_increment,
            )
            for candidate in candidates
        ],
        "decision": serialized_decision,
        "command": command_card,
        "direction_sentiment": direction_sentiment,
        "trade_setup": trade_setup,
        "lifecycle": active_lifecycle,
        "vsa_volume": aligned_vsa,
        "indicators": {
            indicator_id: snapshot_indicator_bundle[indicator_id]
            for indicator_id in indicator_ids()
            if indicator_id in snapshot_indicator_bundle
            and inputs.runtime_params.enabled.get(indicator_id, False)
        },
        "levels": core_levels,
    }
    if gex_context_active:
        snapshot["gex_dynamics"] = build_gex_dynamics_context(
            inputs.confirmed_bars,
            gex_history,
            capture_mode=gex_capture_mode,
        )
    if indicator_timings is not None:
        snapshot["meta"]["indicator_timings_ms"] = dict(
            sorted(indicator_timings.items(), key=lambda item: item[1], reverse=True)
        )
    return _finalize_analysis_snapshot(
        snapshot,
        serialize_output=serialize_output,
    )
