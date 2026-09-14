from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from aef_terminal.domain import Bar, StrategyMode
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.indicators.contracts import is_exact_utc_indicator_timestamp
from aef_terminal.indicators.module_contract import IndicatorPipelineStage
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.mtf import ProviderBarSlotSequence


@dataclass(frozen=True)
class IndicatorRuntimeParams:
    enabled: Mapping[str, bool]
    by_indicator: Mapping[str, Any]
    errors: Mapping[str, str]
    strategy_mode: StrategyMode

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, bool)
            for key, value in self.enabled.items()
        ):
            raise TypeError("indicator runtime enabled contract must be dict[str, bool]")
        if not isinstance(self.by_indicator, Mapping) or any(
            not isinstance(key, str) for key in self.by_indicator
        ):
            raise TypeError("indicator runtime params contract must be keyed by indicator id")
        if not isinstance(self.errors, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.errors.items()
        ):
            raise TypeError("indicator runtime errors contract must be dict[str, str]")
        if not isinstance(self.strategy_mode, StrategyMode):
            raise TypeError("indicator runtime strategy_mode must be StrategyMode")
        enabled_ids = set(self.enabled)
        parameter_ids = set(self.by_indicator)
        if enabled_ids != parameter_ids:
            raise ValueError("indicator runtime enabled and parameter ids must match exactly")
        if not set(self.errors).issubset(parameter_ids):
            raise ValueError("indicator runtime error ids must belong to the parameter contract")
        object.__setattr__(self, "enabled", MappingProxyType(dict(self.enabled)))
        object.__setattr__(self, "by_indicator", MappingProxyType(dict(self.by_indicator)))
        object.__setattr__(self, "errors", MappingProxyType(dict(self.errors)))

    def for_indicator(self, indicator_id: str) -> Any:
        return self.by_indicator[indicator_id]


@dataclass(frozen=True)
class IndicatorExecutionSpec:
    id: str
    input_bars: Sequence[Bar]
    analysis_bar: Bar | None
    mode: str
    calculate: Callable[[], dict[str, Any]] | None = None
    params: Any = None
    runtime_params: IndicatorRuntimeParams | None = None
    promote: bool = True
    postprocess: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    depends_on: Sequence[str] = ()
    calculate_with_context: Callable[[Mapping[str, dict[str, Any]]], dict[str, Any]] | None = None
    preview_calculate: Callable[[], dict[str, Any]] | None = None
    preview_calculate_with_context: (
        Callable[
            [Mapping[str, dict[str, Any]]],
            dict[str, Any],
        ]
        | None
    ) = None
    preview_event_ts: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_params, IndicatorRuntimeParams):
            raise TypeError(f"{self.id} runtime_params must be IndicatorRuntimeParams")
        if self.runtime_params.enabled.get(self.id) is not True:
            raise ValueError(f"{self.id} must be Calc-enabled in runtime_params")
        calculation_count = sum(
            callback is not None for callback in (self.calculate, self.calculate_with_context)
        )
        if calculation_count != 1:
            raise ValueError(
                f"{self.id} must provide exactly one of calculate or calculate_with_context"
            )
        if self.preview_calculate is not None and self.preview_calculate_with_context is not None:
            raise ValueError(f"{self.id} must provide at most one preview calculation contract")
        if not isinstance(self.preview_event_ts, str):
            raise TypeError(f"{self.id} preview_event_ts must be a string")
        if self.preview_event_ts and not is_exact_utc_indicator_timestamp(self.preview_event_ts):
            raise ValueError(f"{self.id} preview_event_ts must be an aware UTC timestamp")
        if self.preview_event_ts and (
            self.preview_calculate is None and self.preview_calculate_with_context is None
        ):
            raise ValueError(f"{self.id} preview_event_ts requires a preview calculation contract")
        for field_name in (
            "calculate",
            "calculate_with_context",
            "postprocess",
            "preview_calculate",
            "preview_calculate_with_context",
        ):
            value = getattr(self, field_name)
            if value is not None and not callable(value):
                raise TypeError(f"{self.id} {field_name} must be callable or None")


@dataclass(frozen=True)
class IndicatorRunContext:
    confirmed_bars: Sequence[Bar]
    live_signal_bars: Sequence[Bar]
    latest: Bar | None
    analysis_latest: Bar | None
    runtime_params: IndicatorRuntimeParams
    indicator_params: dict[str, Any]
    instrument_profile: InstrumentProfile
    features: dict[str, Any]
    analysis_as_of_utc: datetime | None = None
    instrument_id: str = ""
    route_fingerprint: str = ""
    provider: str = ""
    manual_channel_drawings: Sequence[Mapping[str, Any]] = ()
    confirmed_slots: ProviderBarSlotSequence | None = None
    vwap_session: ProviderSessionReset | None = None
    live_preview_active: bool = False
    option_flow: dict[str, Any] | None = None
    tick_flow: dict[str, Any] | None = None
    structure_bars: Sequence[Bar] | None = None
    structure_offset: int = 0
    security_context: dict[str, list[Bar]] = field(default_factory=dict)
    security_context_quality: dict[str, dict[str, Any]] = field(default_factory=dict)
    confirmed_bar_context: dict[str, list[Bar]] = field(default_factory=dict)
    confirmed_bar_context_slots: dict[str, ProviderBarSlotSequence] = field(default_factory=dict)
    confirmed_bar_context_quality: dict[str, dict[str, Any]] = field(default_factory=dict)
    line_width: float = 1.0
    show_geometry: bool = True
    feature_context: Any | None = None
    pivot_context: Any | None = None
    atr_value: float = 0.0
    indicator_bundle: dict[str, dict[str, Any]] = field(default_factory=dict)
    display: dict[str, bool] = field(default_factory=dict)
    decision: Any = None
    decision_candidates: Sequence[Any] = ()
    quality: dict[str, Any] = field(default_factory=dict)
    option_targets: Sequence[dict[str, Any]] | None = None
    shared: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_params, IndicatorRuntimeParams):
            raise TypeError("indicator run context runtime_params must be IndicatorRuntimeParams")

    def params(self, indicator_id: str) -> Any:
        return self.runtime_params.for_indicator(indicator_id)


SpecBuilder = Callable[[IndicatorRunContext], IndicatorExecutionSpec | None]


@dataclass(frozen=True)
class IndicatorModuleAdapter:
    id: str
    stage: IndicatorPipelineStage
    build_spec: SpecBuilder


def calc_enabled(ctx: IndicatorRunContext, indicator_id: str) -> bool:
    return bool(ctx.runtime_params.enabled.get(indicator_id, False))


def unavailable_indicator_result(
    empty_result: Mapping[str, Any],
    vwap_session: ProviderSessionReset | None,
) -> dict[str, Any]:
    result = dict(empty_result)
    reason_code = (
        vwap_session.reason_code if vwap_session is not None else "provider_session_missing"
    )
    result["availability"] = {
        "state": "blocked",
        "reason_code": reason_code,
        "required_context": "provider_vwap_session",
        "vwap": (
            vwap_session.status_payload()
            if vwap_session is not None
            else {
                "available": False,
                "source": "provider_session",
                "reason_code": reason_code,
                "calendar": "unknown",
            }
        ),
    }
    return result


def offset_indicator_indices(
    value: Any,
    offset: int,
    *,
    index_fields: frozenset[str] = frozenset({"index", "projected_index", "end_index"}),
) -> Any:
    if offset <= 0:
        return value
    if isinstance(value, list):
        return [
            offset_indicator_indices(
                item,
                offset,
                index_fields=index_fields,
            )
            for item in value
        ]
    if isinstance(value, dict):
        adjusted: dict[str, Any] = {}
        for key, item in value.items():
            if key in index_fields and isinstance(item, int):
                adjusted[key] = item + offset
            else:
                adjusted[key] = offset_indicator_indices(
                    item,
                    offset,
                    index_fields=index_fields,
                )
        return adjusted
    return value


def shared_vsa_facts(ctx: IndicatorRunContext) -> list[Any]:
    facts = ctx.shared.get("vsa_facts")
    return facts if isinstance(facts, list) else []


def shared_market_series(ctx: IndicatorRunContext) -> Any | None:
    return ctx.shared.get("market_series")


def shared_vix_context(ctx: IndicatorRunContext) -> dict[str, Any] | None:
    block = ctx.shared.get("vix_context")
    return block if isinstance(block, dict) else None
