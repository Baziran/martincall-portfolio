from __future__ import annotations

from typing import Any

from aef_terminal.domain import StrategyMode
from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.indicators.registry import indicator_enabled, indicator_modules
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.indicators.runtime import IndicatorRuntimeParams
from aef_terminal.runtime.stable_hash import canonical_hash_value


def build_indicator_runtime_params(
    indicator_params: dict[str, Any],
    global_defaults: IndicatorDefaults,
) -> IndicatorRuntimeParams:
    modules = indicator_modules()
    enabled = {module.id: indicator_enabled(indicator_params, module.id) for module in modules}
    by_indicator: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for module in modules:
        raw = indicator_params.get(module.id)
        raw_params = dict(raw) if isinstance(raw, dict) else {}
        builder = None
        try:
            if module.params_builder_ref:
                builder = resolve_ref(module.params_builder_ref)
            if builder is not None:
                params = builder(raw_params, global_defaults)
            else:
                params = raw_params
            canonical_hash_value(params)
        except Exception as exc:
            errors[module.id] = f"{type(exc).__name__}: {str(exc)[:240]}"
            try:
                params = builder({}, global_defaults) if builder is not None else {}
                canonical_hash_value(params)
            except Exception:
                params = {}
        by_indicator[module.id] = params

    decision_params = indicator_params.get("decision")
    decision = decision_params if isinstance(decision_params, dict) else {}
    return IndicatorRuntimeParams(
        enabled=enabled,
        by_indicator=by_indicator,
        errors=errors,
        strategy_mode=StrategyMode(decision.get("strategy_mode") or StrategyMode.BALANCED.value),
    )
