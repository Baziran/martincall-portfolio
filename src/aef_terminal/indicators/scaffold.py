from __future__ import annotations

from aef_terminal.indicators.module_contract import validate_indicator_id


def _label_from_id(indicator_id: str) -> str:
    return " ".join(part.capitalize() for part in indicator_id.split("_"))


def _lower_camel(indicator_id: str) -> str:
    parts = indicator_id.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def _kebab(indicator_id: str) -> str:
    return indicator_id.replace("_", "-")


def indicator_module_template(
    indicator_id: str,
    *,
    label: str = "",
    package_name: str = "aef_terminal.indicators.modules",
    pipeline_order: int = 500,
) -> str:
    module_id = validate_indicator_id(indicator_id)
    title = str(label or _label_from_id(module_id)).strip()
    ui_key = _lower_camel(module_id)
    element_prefix = _kebab(module_id)
    module_ref = f"{package_name}.{module_id}"
    return f'''from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.indicators.runtime import IndicatorExecutionSpec, IndicatorRunContext
from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        id="{module_id}",
        label="{title}",
        calculates="Describe what this indicator calculates",
        empty_result={{"version": "0.1-python", "series": [], "events": [], "latest": None, "overlays": []}},
        ui_key="{ui_key}",
        calculate_ref="{module_ref}:{module_id}",
        pipeline_order={int(pipeline_order)},
        signal_source="{module_id}",
        score_family="structure",
        empirical_power=1.0,
        usefulness=1.0,
        state_key="{ui_key}",
        calc_key="{ui_key}CalcEnabled",
        visible_key="{ui_key}Visible",
        chart_control_id="{element_prefix}-toggle",
        process_control_id="{element_prefix}-process",
        api_enabled_key="{module_id}_enabled",
        manager_order={int(pipeline_order)},
        runtime_order={int(pipeline_order)},
        default_calc=False,
    ),
    adapter_ref="{module_ref}:build_execution_spec",
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = {module_id}
    return IndicatorExecutionSpec(
        id="{module_id}",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=lambda: calculate(ctx.confirmed_bars, params=ctx.params("{module_id}")),
        params=ctx.params("{module_id}"),
        runtime_params=ctx.runtime_params,
    )


def {module_id}(bars: Sequence[Bar], *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {{
        "version": "0.1-python",
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
        "settings": params or {{}},
        "status": {{"state_code": "scaffold_wait", "mode": "scaffold"}},
    }}
'''
