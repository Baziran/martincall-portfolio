"""Causal Rounded Top / Rounded Reversal structure indicator.

The package owns two causal tracks under one source. Slow evaluates the frozen
parent-timeframe structure; Fast uses only provider-qualified closed 1m bars for
an early reanchoring watch and micro-structure trigger. The core emits generic
overlays and never promotes a trade candidate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    offset_indicator_indices,
)

from .calculation import _route_key, rounded_reversal
from .contracts import (
    ROUNDED_REVERSAL_MICRO_TIMEFRAME,
    ROUNDED_REVERSAL_SOURCE,
    ROUNDED_REVERSAL_VERSION,
    RoundedReversalParams,
    _ROUNDED_INDEX_FIELDS,
    _ROW_FIELDS,
    build_params,
)
from .fast import fast_rounded_reversal


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "rounded_reversal",
        "Rounded Reversal",
        "dual-speed causal rounded-top exhaustion with adaptive rejection scoring",
        {
            "version": ROUNDED_REVERSAL_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "track_states": [],
            "overlays": [],
            "signals": [],
        },
        "roundedReversal",
        ("aef_terminal.indicators.modules.rounded_reversal:rounded_reversal"),
        group="structure",
        pipeline_stage="structure",
        pipeline_order=105,
        candidate_promoter="none",
        paper_tradable=False,
        optional_context=(
            "absorption_trap",
            "tick_flow",
            "channel_master",
            "market_spotlight",
        ),
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe=ROUNDED_REVERSAL_MICRO_TIMEFRAME,
                history_bars=256,
                role="independent_scan",
            ),
        ),
        shared_context_refs=("vsa_facts", "provider_mtf_quality"),
        renderer_kind="generic",
        renderer_ref="generic_overlay_renderer",
        renderer_primitives=("box", "line"),
        renderer_placements=("price",),
        score_family="reversal",
        empirical_power=0.90,
        usefulness=1.05,
        module_type="context",
        state_key="roundedReversal",
        calc_key="roundedReversalCalcEnabled",
        visible_key="roundedReversalVisible",
        chart_control_id="rounded-reversal-toggle",
        process_control_id="rounded-reversal-process",
        api_enabled_key="rounded_reversal_enabled",
        manager_order=105,
        runtime_order=105,
        default_calc=False,
        default_visible=True,
        show_in_runtime=False,
        overlay_layer="zones",
        runtime_payload_contract={
            "top_level": ("track_states",),
            "latest": _ROW_FIELDS,
            "track_states": _ROW_FIELDS,
            "series": _ROW_FIELDS,
            "events": _ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"limit": 2},
            "track_states": {"limit": 2, "fields": "runtime_contract"},
            "events": {"limit": 20},
            "overlays": {
                "limit": 8,
                "fields": "compact_contract",
            },
        },
        controls=(
            _control(
                "trackMode",
                "Track",
                "select",
                "both",
                "roundedReversalTrackMode",
                element_id="rounded-reversal-track-mode",
                api_key="rounded_reversal_track_mode",
                param_key="track_mode",
                options=("both", "fast", "slow"),
                action="load_apply",
                compact=True,
            ),
            _control(
                "sensitivity",
                "Sensitivity",
                "select",
                "balanced",
                "roundedReversalSensitivity",
                element_id="rounded-reversal-sensitivity",
                api_key="rounded_reversal_sensitivity",
                param_key="sensitivity",
                options=("early", "balanced", "strict"),
                action="load_apply",
                compact=True,
            ),
            _control(
                "mtfRejections",
                "1m tails",
                "toggle",
                True,
                "roundedReversalMtfRejections",
                element_id="rounded-reversal-mtf-rejections",
                api_key="rounded_reversal_use_mtf_rejections",
                param_key="use_mtf_rejections",
                action="load_apply",
                compact=True,
            ),
            _control(
                "microMinRejections",
                "1m hits",
                "number",
                4,
                "roundedReversalMicroMinRejections",
                element_id="rounded-reversal-micro-min-rejections",
                api_key="rounded_reversal_micro_min_rejections",
                param_key="micro_min_rejections",
                minimum=2,
                maximum=12,
                step=1,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref=("aef_terminal.indicators.modules.rounded_reversal:build_execution_spec"),
    params_builder_ref=("aef_terminal.indicators.modules.rounded_reversal:build_params"),
)


def build_execution_spec(
    ctx: IndicatorRunContext,
) -> IndicatorExecutionSpec:
    calculate = rounded_reversal
    structure_bars = ctx.structure_bars or ()
    params = ctx.params("rounded_reversal")
    if not isinstance(params, RoundedReversalParams):
        raise TypeError("rounded_reversal runtime params must be RoundedReversalParams")
    route = _route_key(structure_bars)
    identity = (
        {
            "provider": route[0],
            "instrument_id": route[1],
            "route_fingerprint": route[2],
        }
        if route is not None
        else {}
    )
    indicator_context: dict[str, Any] = {
        "identity": identity,
        "vsa_facts": ctx.shared.get("vsa_facts"),
    }
    for indicator_id in (
        "absorption_trap",
        "channel_master",
        "market_spotlight",
    ):
        payload = ctx.indicator_bundle.get(indicator_id)
        if isinstance(payload, Mapping):
            indicator_context[indicator_id] = payload
    if isinstance(ctx.tick_flow, Mapping):
        indicator_context["tick_flow"] = ctx.tick_flow

    return IndicatorExecutionSpec(
        id="rounded_reversal",
        input_bars=structure_bars,
        analysis_bar=structure_bars[-1] if structure_bars else None,
        mode="confirmed",
        calculate=lambda: _rounded_reversal_with_confirmed_context(
            calculate,
            structure_bars=structure_bars,
            structure_offset=ctx.structure_offset,
            tick_size=ctx.instrument_profile.tick_size,
            params=params,
            confirmed_bar_context=ctx.confirmed_bar_context,
            confirmed_bar_quality=ctx.confirmed_bar_context_quality,
            indicator_context=indicator_context,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        promote=False,
    )


def _rounded_result_source_timeframe(
    result: Mapping[str, Any],
    timeframe: str,
) -> dict[str, Any]:
    tagged = dict(result)
    for section in ("series", "events", "overlays"):
        tagged[section] = [
            {**item, "source_tf": timeframe}
            for item in result.get(section, [])
            if isinstance(item, Mapping)
        ]
    latest = result.get("latest")
    tagged["latest"] = {**latest, "source_tf": timeframe} if isinstance(latest, Mapping) else None
    return tagged


def _rounded_result_order(item: Mapping[str, Any]) -> str:
    return str(item.get("available_at_ts") or item.get("transition_ts") or item.get("ts") or "")


def _rounded_reversal_with_confirmed_context(
    calculate: Any,
    *,
    structure_bars: Sequence[Bar],
    structure_offset: int,
    tick_size: float,
    params: RoundedReversalParams,
    confirmed_bar_context: Mapping[str, Sequence[Bar]],
    confirmed_bar_quality: Mapping[str, Mapping[str, Any]],
    indicator_context: Mapping[str, Any],
) -> dict[str, Any]:
    parent_timeframe = structure_bars[-1].timeframe if structure_bars else ""
    parent_result = (
        calculate(
            structure_bars,
            tick_size=tick_size,
            params=params,
            mtf_context=confirmed_bar_context,
            mtf_quality=confirmed_bar_quality,
            indicator_context=indicator_context,
        )
        if params.track_mode in {"both", "slow"}
        else {
            "version": ROUNDED_REVERSAL_VERSION,
            "settings": {
                "track": "slow",
                "track_mode": params.track_mode,
                "sensitivity": params.sensitivity,
                "disabled_by_track_mode": True,
            },
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "signals": [],
        }
    )
    parent_result = offset_indicator_indices(
        parent_result,
        structure_offset,
        index_fields=_ROUNDED_INDEX_FIELDS,
    )
    parent_result = _rounded_result_source_timeframe(
        parent_result,
        parent_timeframe,
    )

    micro_bars = list(
        confirmed_bar_context.get(
            ROUNDED_REVERSAL_MICRO_TIMEFRAME,
            (),
        )
    )
    micro_quality = dict(
        confirmed_bar_quality.get(
            ROUNDED_REVERSAL_MICRO_TIMEFRAME,
            {},
        )
    )
    results = [parent_result]
    fast_result: Mapping[str, Any] | None = None
    fast_blocked_availability: dict[str, Any] | None = None
    if params.track_mode in {"both", "fast"} and micro_bars and micro_quality.get("ok") is True:
        fast_result = fast_rounded_reversal(
            micro_bars,
            tick_size=tick_size,
            params=params,
        )
        results.append(
            _rounded_result_source_timeframe(
                fast_result,
                ROUNDED_REVERSAL_MICRO_TIMEFRAME,
            )
        )
    elif params.track_mode in {"both", "fast"}:
        fast_blocked_availability = {
            "state": "blocked",
            "reason_code": (
                "rounded_reversal_fast_context_not_admitted"
                if micro_bars
                else "rounded_reversal_fast_context_absent"
            ),
            "timeframe": ROUNDED_REVERSAL_MICRO_TIMEFRAME,
            "quality": micro_quality,
        }

    series = sorted(
        [
            item
            for result in results
            for item in result.get("series", [])
            if isinstance(item, Mapping)
        ],
        key=_rounded_result_order,
    )
    events = sorted(
        [
            item
            for result in results
            for item in result.get("events", [])
            if isinstance(item, Mapping)
        ],
        key=_rounded_result_order,
    )
    settings = dict(parent_result.get("settings") or {})
    settings["confirmed_bar_context"] = {
        ROUNDED_REVERSAL_MICRO_TIMEFRAME: {
            "role": "independent_scan",
            "bar_count": len(micro_bars),
            "admitted": micro_quality.get("ok") is True,
            "quality": micro_quality,
        }
    }
    track_states = [
        item for result in results for item in [result.get("latest")] if isinstance(item, Mapping)
    ]
    track_states.sort(
        key=lambda item: (
            str(item.get("track") or ""),
            _rounded_result_order(item),
        )
    )
    combined = {
        **parent_result,
        "settings": settings,
        "series": series,
        "events": events,
        "latest": series[-1] if series else None,
        "track_states": track_states,
        "overlays": [
            item
            for result in results
            for item in result.get("overlays", [])
            if isinstance(item, Mapping)
        ],
        "signals": [
            item
            for result in results
            for item in result.get("signals", [])
            if isinstance(item, Mapping)
        ],
    }
    if series:
        combined.pop("availability", None)
    elif params.track_mode == "fast":
        if fast_result is not None and isinstance(fast_result.get("availability"), Mapping):
            combined["availability"] = dict(fast_result["availability"])
        elif fast_blocked_availability is not None:
            combined["availability"] = fast_blocked_availability
    return combined


__all__ = [
    "INDICATOR_MODULE",
    "ROUNDED_REVERSAL_MICRO_TIMEFRAME",
    "ROUNDED_REVERSAL_SOURCE",
    "ROUNDED_REVERSAL_VERSION",
    "RoundedReversalParams",
    "build_execution_spec",
    "build_params",
    "rounded_reversal",
]
