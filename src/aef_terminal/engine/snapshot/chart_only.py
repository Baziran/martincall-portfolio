from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.providers import (
    default_data_source,
    normalize_provider_key,
    provider_data_policy,
)
from aef_terminal.data.instrument_identity import (
    current_futures_contract_id,
    instrument_is_futures,
    instrument_provider,
    provider_price_increment,
    provider_contract_id,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.domain import (
    ActionPhase,
    Bar,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
)
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.engine.analysis_db import (
    chart_guide_calculation_bars,
)
from aef_terminal.engine.data_quality import (
    data_quality_report,
    serialize_data_quality_report,
    snapshot_freshness,
)
from aef_terminal.engine.serialization import (
    serialize_bars,
    serialize_decision,
)
from aef_terminal.engine.vsa_context import (
    VSA_VOLUME_RENDER_MAX_ROWS,
    bound_vsa_volume_render_context,
    build_vsa_volume_context_for_instrument,
)
from aef_terminal.features.chart_guides import build_chart_guides
from aef_terminal.features.context import build_feature_context
from aef_terminal.features.provider_session import provider_vwap_session
from aef_terminal.indicators.defaults import indicator_defaults_from_params
from aef_terminal.runtime.overlays import futures_roll_event_overlays

from aef_terminal.engine.snapshot.empty import empty_market_snapshot
from aef_terminal.engine.snapshot.db_context import (
    snapshot_db_context,
    chart_future_axis_payload,
    provider_chart_axis_slots,
    snapshot_futures_roll_events,
)


_VSA_CHART_CALCULATION_WARMUP_BARS = 200


def chart_only_market_snapshot(
    instrument: dict[str, str],
    chart_symbol: str,
    interval: str,
    range_: str,
    bars: Sequence[Bar],
    warning: str = "",
    *,
    history_coverage: dict[str, Any],
    canonical_generation: int,
    signal_range: str | None = None,
    data_provider: str = default_data_source(),
    store: Any | None = None,
    indicator_params: dict[str, Any] | None = None,
    guide_warmup_bars: Sequence[Bar] = (),
    guide_warmup_failed: bool = False,
) -> dict[str, Any]:
    if (
        isinstance(canonical_generation, bool)
        or not isinstance(canonical_generation, int)
        or canonical_generation < 0
    ):
        raise ValueError("canonical_generation must be a non-negative integer")
    bar_list = list(bars)
    provider_key = normalize_provider_key(data_provider)
    if not bar_list:
        provider_label = provider_key.upper()
        snapshot = empty_market_snapshot(
            instrument, interval, warning or f"No cached {provider_label} bars available."
        )
        snapshot["meta"]["history_coverage"] = dict(history_coverage)
        snapshot["meta"]["chart_canonical_revision"] = canonical_generation
        return snapshot
    (
        bar_list,
        bar_slots,
        _mtf_context,
        _mtf_context_slots,
        _mtf_quality,
    ) = snapshot_db_context(
        store,
        chart_symbol,
        interval,
        range_,
        bar_list,
        provider_key=provider_key,
        instrument=instrument,
        include_mtf=False,
    )
    if not bar_list:
        provider_label = provider_key.upper()
        snapshot = empty_market_snapshot(
            instrument,
            interval,
            warning or f"No canonical {provider_label} bars remain in the requested window.",
        )
        snapshot["meta"]["history_coverage"] = dict(history_coverage)
        snapshot["meta"]["chart_canonical_revision"] = canonical_generation
        return snapshot
    delay_grace = provider_data_policy(provider_key).tail_delay_grace_seconds
    quality = data_quality_report(
        bar_list,
        interval,
        tail_delay_grace_seconds=delay_grace,
        store=store,
        instrument=instrument,
    )
    serialized = serialize_bars(
        bar_list,
        bar_slots,
        session_reset=provider_vwap_session(instrument, bar_list),
    )
    latest = bar_list[-1]
    previous = bar_list[-2] if len(bar_list) > 1 else None
    change = latest.close - previous.close if previous is not None else 0.0
    change_pct = (
        (change / previous.close * 100.0) if previous is not None and previous.close else 0.0
    )
    source = str(latest.source or f"{provider_key}:db-cache")
    instrument_profile = resolve_instrument_profile(instrument)
    guide_calculation_bars = chart_guide_calculation_bars(
        (),
        bar_list,
        interval=interval,
        warmup_bars=guide_warmup_bars,
    )
    chart_guides = build_chart_guides(
        guide_calculation_bars,
        symbol=instrument["display"],
        feature_context=build_feature_context(
            guide_calculation_bars,
            instrument_profile=instrument_profile,
            defaults=indicator_defaults_from_params(
                indicator_params.get("global_defaults")
                if isinstance(indicator_params, dict)
                and isinstance(indicator_params.get("global_defaults"), dict)
                else None
            ),
        ),
        instrument=instrument,
        display_axis=serialized,
        context_bars=bar_list,
        magnet_warmup_failed=guide_warmup_failed,
    )
    guide_slots = (
        list(bar_slots)
        if (
            bar_slots is not None
            and bar_slots.authoritative is True
            and len(bar_slots) == len(guide_calculation_bars)
            and all(
                chart_bar == guide_bar
                for chart_bar, guide_bar in zip(
                    bar_list,
                    guide_calculation_bars,
                    strict=True,
                )
            )
        )
        else None
    )
    if guide_slots is None:
        try:
            guide_slots = provider_chart_axis_slots(
                store,
                instrument,
                interval,
                guide_calculation_bars,
            )
        except Exception:
            guide_slots = None
    vsa_calculation_limit = _VSA_CHART_CALCULATION_WARMUP_BARS + VSA_VOLUME_RENDER_MAX_ROWS
    vsa_calculation_bars = guide_calculation_bars[-vsa_calculation_limit:]
    vsa_calculation_slots = (
        guide_slots[-len(vsa_calculation_bars) :]
        if guide_slots is not None and len(guide_slots) >= len(vsa_calculation_bars)
        else None
    )
    vsa_volume = build_vsa_volume_context_for_instrument(
        vsa_calculation_bars,
        instrument,
        indicator_params=indicator_params,
        confirmed_slots=vsa_calculation_slots,
    )
    vsa_volume = bound_vsa_volume_render_context(
        vsa_volume,
        latest_ts=latest.ts,
    )
    updated_at = datetime.now(tz=UTC)
    roll_overlays = futures_roll_event_overlays(
        bars=bar_list,
        roll_events=snapshot_futures_roll_events(
            store,
            instrument=instrument,
            bars=bar_list,
            provider_key=provider_key,
        ),
    )
    cache_decision = ScenarioDecision(
        kind=ScenarioKind.WAIT,
        direction=Direction.FLAT,
        confidence=0.0,
        action=ActionPhase.BLOCK,
        trigger=None,
        stop=None,
        target=None,
        invalidation=None,
        trigger_event=DomainFact("chart_cache_only"),
        source="chart_cache",
        reason_codes=["chart_cache_only"],
        metrics={"chart_only": True},
    )
    return {
        "meta": {
            "symbol": instrument["display"],
            "provider_symbol": instrument["provider_symbol"],
            "instrument_id": qualified_instrument_id(instrument),
            "route_fingerprint": route_fingerprint(instrument),
            "provider": instrument_provider(instrument),
            "provider_contract_id": current_futures_contract_id(instrument)
            if instrument_is_futures(instrument)
            else provider_contract_id(instrument),
            "timeframe": interval,
            "source": source,
            "price": latest.close,
            "change": change,
            "change_pct": change_pct,
            "live_bar_closed": bool(latest.closed),
            "confirmed_bar_count": sum(1 for bar in bar_list if bar.closed),
            "instrument_profile": instrument_profile.as_meta(),
            "price_increment": provider_price_increment(instrument),
            "updated_at": updated_at.isoformat(),
            "freshness": snapshot_freshness(
                timeframe=interval,
                source=source,
                latest_bar_ts=latest.ts,
                updated_at=updated_at,
                instrument_profile=instrument_profile,
            ),
            "warning": warning,
            "data_quality": {},
            "chart_data_quality": serialize_data_quality_report(quality),
            "chart_quality_revision": 0,
            "chart_canonical_revision": canonical_generation,
            "history_coverage": dict(history_coverage),
            "chart_range": range_,
            "signal_range": signal_range or range_,
            "chart_bar_count": len(bar_list),
            "signal_bar_count": len(bar_list),
            "chart_only": True,
        },
        "bars": serialized,
        "future_axis": chart_future_axis_payload(
            store,
            interval,
            serialized,
            provider_key=provider_key,
            instrument=instrument,
        ),
        "chart_guides": chart_guides,
        "features": {},
        "candidates": [],
        "indicators": {},
        "vsa_volume": vsa_volume,
        "overlays": roll_overlays,
        "decision": serialize_decision(cache_decision),
        "levels": [],
    }
