from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


IndicatorSettingsScope = Literal["instrument", "global"]
IndicatorControlType = Literal["toggle", "number", "select", "color"]
IndicatorControlAction = Literal["apply", "load", "load_apply", "render", "none"]

INDICATOR_TABLE_POSITION_OPTIONS = (
    "bottom",
    "top",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "dock",
    "off",
)
INDICATOR_TABLE_POSITION_LABELS = (
    "Bottom strip",
    "Top strip",
    "Top left",
    "Top right",
    "Bottom left",
    "Bottom right",
    "Indicator Lens",
    "Off",
)


@dataclass(frozen=True)
class IndicatorControlSpec:
    key: str
    label: str
    control_type: IndicatorControlType
    default: bool | int | float | str
    storage_key: str
    element_id: str = ""
    state_key: str = ""
    api_key: str = ""
    param_key: str = ""
    scope: IndicatorSettingsScope = "instrument"
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    options: tuple[str, ...] = ()
    option_labels: tuple[str, ...] = ()
    action: IndicatorControlAction = "apply"
    effect_ref: str = ""
    compact: bool = False


def _control(
    key: str,
    label: str,
    control_type: IndicatorControlType,
    default: bool | int | float | str,
    storage_key: str,
    *,
    element_id: str = "",
    state_key: str = "",
    api_key: str = "",
    param_key: str = "",
    scope: IndicatorSettingsScope = "instrument",
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    step: float | int | None = None,
    options: tuple[str, ...] = (),
    option_labels: tuple[str, ...] = (),
    action: IndicatorControlAction = "apply",
    effect_ref: str = "",
    compact: bool = False,
) -> IndicatorControlSpec:
    return IndicatorControlSpec(
        key=key,
        label=label,
        control_type=control_type,
        default=default,
        storage_key=storage_key,
        element_id=element_id,
        state_key=state_key or key,
        api_key=api_key,
        param_key=param_key or state_key or key,
        scope=scope,
        minimum=minimum,
        maximum=maximum,
        step=step,
        options=options,
        option_labels=option_labels,
        action=action,
        effect_ref=effect_ref,
        compact=compact,
    )


def indicator_table_controls(
    storage_prefix: str,
    element_prefix: str,
    *,
    default_position: str = "bottom",
    default_opacity: int = 90,
    scope: IndicatorSettingsScope = "instrument",
) -> tuple[IndicatorControlSpec, IndicatorControlSpec]:
    """Declare UI-only table placement and density controls consistently."""

    if default_position not in INDICATOR_TABLE_POSITION_OPTIONS:
        raise ValueError(f"Unsupported indicator table position: {default_position}")
    return (
        _control(
            "tablePosition",
            "Table",
            "select",
            default_position,
            f"{storage_prefix}TablePosition",
            element_id=f"{element_prefix}-table-position",
            options=INDICATOR_TABLE_POSITION_OPTIONS,
            option_labels=INDICATOR_TABLE_POSITION_LABELS,
            action="render",
            effect_ref="table_position_state",
            compact=True,
            scope=scope,
        ),
        _control(
            "tableOpacity",
            "Table opacity %",
            "number",
            default_opacity,
            f"{storage_prefix}TableOpacity",
            element_id=f"{element_prefix}-table-opacity",
            minimum=65,
            maximum=100,
            step=5,
            action="render",
            compact=True,
            scope=scope,
        ),
    )
