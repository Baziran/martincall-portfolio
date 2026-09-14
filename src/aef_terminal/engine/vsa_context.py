from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from time import perf_counter
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    provider_vwap_session,
)
from aef_terminal.features.vsa import VsaFact, VsaParams, vsa_facts
from aef_terminal.features.vsa_classify import (
    VSA_BREAKOUT_CONTEXT_CONTRACT,
    VsaBreakoutFact,
    series_item_from_fact,
    vsa_breakout_fact_payload,
    vsa_breakout_facts,
)
from aef_terminal.indicators.contracts import normalize_overlay_item
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    IndicatorDefaults,
    indicator_defaults_from_params,
    label_importance_floor,
)
from aef_terminal.indicators.domain_facts import (
    INDICATOR_FACT_RUNTIME_FIELDS,
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.indicators.module_contract import (
    OVERLAY_COMPACT_FIELDS,
    OVERLAY_RUNTIME_FIELDS,
)
from aef_terminal.runtime import overlays
from aef_terminal.runtime.instruments import (
    InstrumentProfile,
    resolve_instrument_profile,
)
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.pine import PineContext


VSA_VOLUME_CONTRACT = "vsa-volume-context-v1"
VSA_VOLUME_VERSION = "3-vsa-breakout-context"


class VsaVolumeRenderHours(IntEnum):
    TWO_HOURS = 2
    FOUR_HOURS = 4
    SIX_HOURS = 6
    TWELVE_HOURS = 12


VSA_VOLUME_RENDER_HOUR_OPTIONS = tuple(item.value for item in VsaVolumeRenderHours)
VSA_VOLUME_RENDER_DEFAULT_HOURS = VsaVolumeRenderHours.SIX_HOURS.value
VSA_VOLUME_RENDER_MAX_HOURS = max(VSA_VOLUME_RENDER_HOUR_OPTIONS)
VSA_VOLUME_RENDER_WINDOW_SECONDS = VSA_VOLUME_RENDER_MAX_HOURS * 60 * 60
VSA_VOLUME_RENDER_MAX_ROWS = VSA_VOLUME_RENDER_MAX_HOURS * 60
VSA_VOLUME_COMPACT_ROW_FIELDS = (
    "absorption_role",
    "authoritative",
    "availability_state",
    "bar_slot",
    "base_code",
    "breakout_context",
    "code",
    "direction",
    "display_avg",
    "display_volume",
    "fuel",
    "move_atr",
    "range_atr",
    "reason_code",
    "rvol",
    "score",
    "spread_rel",
    "structure_text_ref",
    "structure_text_role",
    "terminal_climax",
    "terminal_direction",
    "terminal_kind",
    "ts",
    "vol_rank",
    "vol_z",
    "vsa_role",
)
VSA_VOLUME_ROW_FIELDS = (
    "absorption",
    "absorption_level",
    "absorption_role",
    "authoritative",
    "availability_state",
    "bar_slot",
    "base_code",
    "breakout_context",
    "close_auction",
    "code",
    "direction",
    "display_avg",
    "display_volume",
    "fuel",
    "fuel_late_trend",
    "important",
    "move_atr",
    "near_resistance",
    "near_support",
    "range_atr",
    "reason_code",
    "rvol",
    "score",
    "spread_rel",
    "spring",
    "structure_text_ref",
    "structure_text_role",
    "terminal_climax",
    "terminal_direction",
    "terminal_kind",
    "ts",
    "upthrust",
    "vol_rank",
    "vol_z",
    "vsa_role",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)
VSA_VOLUME_RUNTIME_CONTRACT = {
    "latest": VSA_VOLUME_ROW_FIELDS,
    "series": VSA_VOLUME_ROW_FIELDS,
    "events": VSA_VOLUME_ROW_FIELDS,
    "overlays": tuple(OVERLAY_RUNTIME_FIELDS),
}
VSA_VOLUME_COMPACT_CONTRACT = {
    "series": VSA_VOLUME_COMPACT_ROW_FIELDS,
    "events": VSA_VOLUME_COMPACT_ROW_FIELDS,
    "overlays": tuple(OVERLAY_COMPACT_FIELDS),
}
VSA_VOLUME_COMPACT_POLICY = {
    "series": {"limit": VSA_VOLUME_RENDER_MAX_ROWS},
    "events": {"limit": 80},
    "overlays": {"limit": 80},
}


def _vsa_row_timestamp(item: Mapping[str, Any]) -> datetime | None:
    raw = item.get("ts") or item.get("start_ts") or item.get("end_ts")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _vsa_mapping_rows(value: Any) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def normalize_vsa_volume_render_hours(value: Any, *, default: int) -> int:
    try:
        hours = int(value)
    except TypeError, ValueError:
        hours = int(default)
    return hours if hours in VSA_VOLUME_RENDER_HOUR_OPTIONS else int(default)


def bound_vsa_volume_render_context(
    context: Mapping[str, Any],
    *,
    latest_ts: datetime | None = None,
    render_hours: int = VSA_VOLUME_RENDER_MAX_HOURS,
) -> dict[str, Any]:
    """Project calculated VSA facts onto the bounded browser render tail."""

    result = dict(context)
    series = [dict(item) for item in _vsa_mapping_rows(context.get("series"))]
    resolved_latest = (
        latest_ts.astimezone(UTC)
        if latest_ts is not None and latest_ts.tzinfo is not None
        else None
    )
    if resolved_latest is None:
        latest = context.get("latest")
        if isinstance(latest, Mapping):
            resolved_latest = _vsa_row_timestamp(latest)
    if resolved_latest is None:
        timestamps = [timestamp for item in series if (timestamp := _vsa_row_timestamp(item))]
        resolved_latest = max(timestamps, default=None)
    resolved_render_hours = normalize_vsa_volume_render_hours(
        render_hours,
        default=VSA_VOLUME_RENDER_MAX_HOURS,
    )
    render_window_seconds = resolved_render_hours * 60 * 60
    cutoff = (
        resolved_latest - timedelta(seconds=render_window_seconds)
        if resolved_latest is not None
        else None
    )

    def bounded_rows(key: str) -> list[dict[str, Any]]:
        rows = [
            dict(item)
            for item in _vsa_mapping_rows(context.get(key))
            if (cutoff is None or (row_ts := _vsa_row_timestamp(item)) is None or row_ts > cutoff)
        ]
        limit = int(VSA_VOLUME_COMPACT_POLICY[key]["limit"])
        return rows[-limit:]

    for section in VSA_VOLUME_COMPACT_POLICY:
        result[section] = bounded_rows(section)

    breakout = context.get("breakout_context")
    if isinstance(breakout, Mapping):
        breakout_result = dict(breakout)
        breakout_events = [
            dict(item)
            for item in _vsa_mapping_rows(breakout.get("events"))
            if (cutoff is None or (row_ts := _vsa_row_timestamp(item)) is None or row_ts > cutoff)
        ]
        breakout_result["events"] = breakout_events[-80:]
        result["breakout_context"] = breakout_result

    status = context.get("status")
    if isinstance(status, Mapping):
        result["status"] = {
            **status,
            "series_count": len(result["series"]),
            "render_window_seconds": render_window_seconds,
            "render_window_max_rows": resolved_render_hours * 60,
        }
    return result


@dataclass(frozen=True)
class VsaVolumeParams(VsaParams):
    use_profile_thresholds: bool = True
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    importance_floor: float = 72.0


def build_vsa_volume_params(
    defaults: IndicatorDefaults,
    profile: InstrumentProfile,
) -> VsaVolumeParams:
    params = VsaVolumeParams(
        atr_len=defaults.atr_len,
        vol_len=defaults.rvol_len,
        importance_floor=label_importance_floor(defaults.score),
    )
    return profile_adjusted_vsa_params(profile, params)[0]


def profile_adjusted_vsa_params(
    profile: InstrumentProfile,
    params: VsaVolumeParams,
) -> tuple[VsaVolumeParams, str]:
    if not params.use_profile_thresholds:
        return params, profile.label
    return (
        replace(
            params,
            impulse_rvol=profile.vsa_impulse_rvol,
            fuel_rvol=profile.vsa_fuel_rvol,
            fuel_range_atr=profile.fuel_range_atr,
        ),
        profile.label,
    )


def align_vsa_volume_series(
    series: Sequence[Mapping[str, Any]],
    display_axis: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Publish VSA render facts on the exact terminal-owned display axis."""

    by_ts = {str(item.get("ts") or ""): dict(item) for item in series if str(item.get("ts") or "")}
    aligned: list[dict[str, Any]] = []
    for bar in display_axis:
        ts = str(bar.get("ts") or "")
        item = dict(by_ts.get(ts) or {})
        authoritative = bool(
            bar.get("authoritative") is not False
            and bar.get("closed") is True
            and str(bar.get("state") or "confirmed") == "confirmed"
        )
        item["ts"] = ts
        item["bar_slot"] = bar.get("bar_slot")
        item["authoritative"] = authoritative
        item["availability_state"] = str(
            bar.get("availability_state")
            or ("confirmed" if authoritative else bar.get("state") or "provisional")
        )
        if not by_ts.get(ts):
            item["display_volume"] = None
            item["display_avg"] = None
        aligned.append(item)
    return aligned


def empty_vsa_volume_context(
    reason_code: str = "no_confirmed_bars",
) -> dict[str, Any]:
    calculated_at = datetime.now(tz=UTC).isoformat()
    return {
        "contract": VSA_VOLUME_CONTRACT,
        "version": VSA_VOLUME_VERSION,
        "series": [],
        "events": [],
        "overlays": [],
        "latest": None,
        "breakout_context": {
            "contract": VSA_BREAKOUT_CONTEXT_CONTRACT,
            "mode": "context_only",
            "policy": "neutral",
            "latest": None,
            "events": [],
            "availability": {
                "state": "blocked",
                "reason_code": str(reason_code or "no_confirmed_bars"),
            },
        },
        "params": {},
        "availability": {
            "state": "blocked",
            "reason_code": str(reason_code or "no_confirmed_bars"),
        },
        "status": {
            "state": "blocked",
            "reason_code": str(reason_code or "no_confirmed_bars"),
            "mode": "confirmed",
            "bar_count": 0,
            "series_count": 0,
            "event_count": 0,
            "analysis_ts": None,
            "calculated_at": calculated_at,
            "elapsed_ms": 0.0,
            "last_error": None,
        },
    }


def build_vsa_volume_context(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    params: VsaVolumeParams | None = None,
    facts: Sequence[VsaFact] | None = None,
    breakout_facts: Sequence[VsaBreakoutFact] | None = None,
    confirmed_slots: Sequence[int] | None = None,
    vwap_session: ProviderSessionReset | None = None,
) -> dict[str, Any]:
    """Build the engine-owned VSA context from exchange-confirmed bars only."""

    started = perf_counter()
    calculated_at = datetime.now(tz=UTC).isoformat()
    confirmed_bars = list(bars)
    if not confirmed_bars:
        return empty_vsa_volume_context()
    if len(PineContext.from_bars(confirmed_bars).confirmed_bars) != len(confirmed_bars):
        failed = empty_vsa_volume_context("unconfirmed_bar_input")
        return {
            **failed,
            "status": {
                **failed["status"],
                "state": "error",
                "bar_count": len(confirmed_bars),
                "calculated_at": calculated_at,
                "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
                "last_error": "VSA core accepts exchange-confirmed bars only",
            },
        }

    resolved_params, profile_label = profile_adjusted_vsa_params(
        profile,
        params or VsaVolumeParams(),
    )
    try:
        if facts is not None:
            resolved_facts = list(facts)
            if len(resolved_facts) != len(confirmed_bars) or any(
                not isinstance(fact, VsaFact) or fact.index != index or fact.bar != bar
                for index, (fact, bar) in enumerate(
                    zip(
                        resolved_facts,
                        confirmed_bars,
                        strict=False,
                    )
                )
            ):
                raise ValueError("VSA facts must match the exact confirmed-bar input")
        else:
            resolved_facts = vsa_facts(
                confirmed_bars,
                resolved_params,
                vwap_session=vwap_session,
                confirmed_slots=confirmed_slots,
            )
        if breakout_facts is not None:
            resolved_breakout_facts = list(breakout_facts)
            if len(resolved_breakout_facts) != len(confirmed_bars) or any(
                not isinstance(fact, VsaBreakoutFact) or fact.index != index or fact.bar != bar
                for index, (fact, bar) in enumerate(
                    zip(
                        resolved_breakout_facts,
                        confirmed_bars,
                        strict=False,
                    )
                )
            ):
                raise ValueError("VSA breakout facts must match the exact confirmed-bar input")
        else:
            resolved_breakout_facts = vsa_breakout_facts(
                confirmed_bars,
                resolved_facts,
                profile_key=profile.key,
                vwap_session=vwap_session,
                confirmed_slots=confirmed_slots,
            )
        series: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        overlay_items: list[dict[str, Any]] = []
        breakout_events: list[dict[str, Any]] = []
        for fact, breakout_fact in zip(
            resolved_facts,
            resolved_breakout_facts,
            strict=True,
        ):
            item = series_item_from_fact(
                fact,
                importance_floor=resolved_params.importance_floor,
            )
            breakout_payload = vsa_breakout_fact_payload(breakout_fact)
            has_breakout_context = bool(
                breakout_fact.long_break
                or breakout_fact.short_break
                or breakout_fact.long_disposition != "neutral"
                or breakout_fact.short_disposition != "neutral"
            )
            item.update(
                {
                    "breakout_context": (breakout_payload if has_breakout_context else None),
                    "display_volume": _round(fact.display_volume),
                    "display_avg": _round(fact.display_avg),
                    **indicator_fact_payload(
                        scenario="vsa_volume",
                        trigger_event={
                            "code": str(
                                fact.terminal_kind
                                if fact.terminal_climax
                                else item.get("code") or "volume_summary"
                            ).lower()
                        },
                        quality={"code": "score", "value": item["score"]},
                        metrics={
                            "score": _metric_number(fact.score, digits=0),
                            "terminal_climax": bool(fact.terminal_climax),
                            "terminal_kind": (fact.terminal_kind if fact.terminal_climax else ""),
                            "terminal_direction": (
                                fact.terminal_direction if fact.terminal_climax else ""
                            ),
                            "close_auction_compressed": bool(fact.close_auction),
                            "rvol": _metric_number(fact.rvol),
                            "volume_rank": _metric_number(
                                fact.vol_rank,
                                digits=0,
                                suffix="%",
                            ),
                            "volume_z": _metric_number(fact.vol_z),
                            "move_atr": _metric_number(
                                fact.move_atr,
                                suffix=" ATR",
                            ),
                            "range_atr": _metric_number(
                                fact.range_atr,
                                suffix=" ATR",
                            ),
                            "spread": _metric_number(fact.spread_rel),
                        },
                    ),
                }
            )
            series.append(item)
            if item.get("code") or item.get("terminal_climax"):
                events.append(item)
            if has_breakout_context and (
                breakout_fact.long_disposition != "neutral"
                or breakout_fact.short_disposition != "neutral"
            ):
                breakout_events.append(breakout_payload)
            if item.get("important") and item.get("code"):
                direction = str(item.get("direction") or "flat")
                bar = fact.bar
                mark = overlays.label(
                    bar=bar,
                    price=(
                        bar.low
                        if direction == "long"
                        else bar.high
                        if direction == "short"
                        else bar.close
                    ),
                    lines=[],
                    direction=direction,
                    side=(
                        "below"
                        if direction == "long"
                        else "above"
                        if direction == "short"
                        else "center"
                    ),
                    role="vsa_price_mark",
                    fact_fields=copy_indicator_facts(item),
                )
                mark.update(
                    {
                        "code": item["code"],
                        "base_code": item.get("base_code"),
                        "score": item.get("score"),
                        "rvol": item.get("rvol"),
                        "vol_rank": item.get("vol_rank"),
                        "vol_z": item.get("vol_z"),
                        "move_atr": item.get("move_atr"),
                        "range_atr": item.get("range_atr"),
                        "spread_rel": item.get("spread_rel"),
                        "fuel": item.get("fuel"),
                        "terminal_climax": item.get("terminal_climax"),
                        "terminal_kind": item.get("terminal_kind"),
                        "terminal_direction": item.get("terminal_direction"),
                        "absorption_role": item.get("absorption_role"),
                        "structure_text_role": "vsa",
                        "overlay_role": "vsa_price_mark",
                        "control_key": "priceMarks",
                    }
                )
                overlay_items.append(normalize_overlay_item(mark, source="vsa_volume"))
    except Exception as exc:
        failed = empty_vsa_volume_context("vsa_calculation_error")
        failed["status"] = {
            **failed["status"],
            "state": "error",
            "bar_count": len(confirmed_bars),
            "analysis_ts": confirmed_bars[-1].ts.isoformat(),
            "calculated_at": calculated_at,
            "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
            "last_error": str(exc),
        }
        return failed

    session_degraded = vwap_session is None or not vwap_session.available
    slot_degraded = confirmed_slots is None
    degraded = session_degraded or slot_degraded
    reason_code = (
        (vwap_session.reason_code or "provider_session_unavailable")
        if vwap_session is not None and session_degraded
        else "provider_session_missing"
        if session_degraded
        else "provider_slot_axis_unavailable"
        if slot_degraded
        else "ok"
    )
    missing_context = (
        "provider_vwap_session"
        if session_degraded
        else "canonical_provider_bar_slots"
        if slot_degraded
        else None
    )
    state = "degraded" if degraded else "ready"
    result = {
        "contract": VSA_VOLUME_CONTRACT,
        "version": VSA_VOLUME_VERSION,
        "series": series,
        "events": events,
        "overlays": overlay_items,
        "latest": series[-1] if series else None,
        "breakout_context": {
            "contract": VSA_BREAKOUT_CONTEXT_CONTRACT,
            "mode": "context_only",
            "policy": (
                resolved_breakout_facts[-1].policy if resolved_breakout_facts else "neutral"
            ),
            "latest": (
                vsa_breakout_fact_payload(resolved_breakout_facts[-1])
                if resolved_breakout_facts
                else None
            ),
            "events": breakout_events[-80:],
            "availability": {
                "state": (
                    resolved_breakout_facts[-1].availability_state
                    if resolved_breakout_facts
                    else "blocked"
                ),
                "reason_code": (
                    resolved_breakout_facts[-1].reason_code
                    if resolved_breakout_facts
                    else "no_confirmed_bars"
                ),
            },
        },
        "params": {
            "profile": profile_label,
            "use_profile_thresholds": resolved_params.use_profile_thresholds,
            "impulse_rvol": resolved_params.impulse_rvol,
            "fuel_rvol": resolved_params.fuel_rvol,
            "fuel_range_atr": resolved_params.fuel_range_atr,
            "vol_len": resolved_params.vol_len,
            "regime_len": resolved_params.regime_len,
            "atr_len": resolved_params.atr_len,
            "use_adaptive": resolved_params.use_adaptive,
        },
        "availability": {
            "state": state,
            "reason_code": reason_code,
            "missing_context": missing_context,
        },
        "status": {
            "state": state,
            "reason_code": reason_code,
            "mode": "confirmed",
            "bar_count": len(confirmed_bars),
            "series_count": len(series),
            "event_count": len(events),
            "analysis_ts": confirmed_bars[-1].ts.isoformat(),
            "calculated_at": calculated_at,
            "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
            "last_error": None,
        },
    }
    return result


def build_vsa_volume_context_for_instrument(
    bars: Sequence[Bar],
    instrument: Mapping[str, Any],
    *,
    indicator_params: Mapping[str, Any] | None = None,
    confirmed_slots: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Resolve the instrument policy once and build its confirmed VSA context."""

    raw_settings = indicator_params if isinstance(indicator_params, Mapping) else {}
    raw_global_defaults = raw_settings.get("global_defaults")
    defaults = indicator_defaults_from_params(
        raw_global_defaults if isinstance(raw_global_defaults, Mapping) else None
    )
    profile = resolve_instrument_profile(dict(instrument))
    params = build_vsa_volume_params(
        defaults,
        profile,
    )
    confirmed_bars = list(PineContext.from_bars(bars).confirmed_bars) if bars else []
    return build_vsa_volume_context(
        confirmed_bars,
        profile=profile,
        params=params,
        confirmed_slots=confirmed_slots,
        vwap_session=(
            provider_vwap_session(dict(instrument), confirmed_bars) if confirmed_bars else None
        ),
    )
