from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.domain import (
    DomainFact,
    domain_wire_value,
    validate_domain_code,
    validate_domain_value,
)


INDICATOR_FACT_FIELD_KEYS = frozenset(
    {
        "scenario",
        "setup",
        "trigger_event",
        "evidence",
        "risk",
        "quality",
        "fact_groups",
        "metrics",
        "trade_plan",
    }
)

INDICATOR_FACT_RUNTIME_FIELDS = tuple(sorted(INDICATOR_FACT_FIELD_KEYS))

_EVIDENCE_GROUPS = frozenset({"supporting", "opposing", "context"})

_SIGNAL_NAME_CODE_GROUPS = {
    "vsa_volume": (
        (
            "vsa_fade",
            frozenset({"SPRING", "UPTHRUST", "EXH_UP", "EXH_DN", "ABS", "ABS_LVL"}),
        ),
        ("vsa_continuation", frozenset({"FUEL_UP", "FUEL_DN", "IMP_UP", "IMP_DN"})),
    ),
}
_SIGNAL_NAME_PREFIX_GROUPS: dict[
    str,
    tuple[tuple[str, tuple[str, ...]], ...],
] = {}
_SIGNAL_NAME_ACTION_GROUPS = {
    "impulse_fib": (("impulse_pullback", frozenset({"WATCH", "ARM"})),),
}
_SIGNAL_NAME_FALLBACKS = {
    "impulse_fib": "impulse",
}


def metric_number(
    value: Any,
    *,
    digits: int = 2,
    suffix: str = "",
) -> dict[str, Any]:
    metric: dict[str, Any] = {"value": value, "digits": digits}
    if suffix:
        metric["suffix"] = suffix
    return metric


def resolve_signal_name(source: str, code: str, action: str) -> str:
    normalized_source = str(source or "")
    normalized_code = str(code or "").upper()
    normalized_action = str(action or "").upper()
    for signal_name, codes in _SIGNAL_NAME_CODE_GROUPS.get(normalized_source, ()):
        if normalized_code in codes:
            return signal_name
    for signal_name, prefixes in _SIGNAL_NAME_PREFIX_GROUPS.get(normalized_source, ()):
        if normalized_code.startswith(prefixes):
            return signal_name
    for signal_name, actions in _SIGNAL_NAME_ACTION_GROUPS.get(normalized_source, ()):
        if normalized_action in actions:
            return signal_name
    return _SIGNAL_NAME_FALLBACKS.get(normalized_source, normalized_source)


def _require_code(value: object, *, field_name: str) -> str:
    return validate_domain_code(value, field_name=field_name)


def _wire_value(value: Any, *, field_name: str) -> Any:
    return domain_wire_value(value, field_name=field_name)


FactInput = DomainFact | Mapping[str, Any]


def _fact(value: FactInput, *, field_name: str) -> DomainFact:
    if isinstance(value, DomainFact):
        return value
    if isinstance(value, Mapping):
        return DomainFact.from_mapping(value)
    raise TypeError(f"{field_name} must be a DomainFact or canonical fact mapping")


def _facts(
    values: FactInput | Sequence[FactInput] | None,
    *,
    field_name: str,
) -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (DomainFact, Mapping)):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{field_name} must be a fact or a sequence of facts")
    return [
        _fact(value, field_name=f"{field_name}[{index}]").as_dict()
        for index, value in enumerate(values)
    ]


def _fact_groups(
    groups: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if groups is None:
        return []
    if isinstance(groups, Mapping):
        groups = [groups]
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes, bytearray)):
        raise TypeError("fact_groups must be a mapping or sequence of mappings")
    normalized: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            raise TypeError(f"fact_groups[{index}] must be a mapping")
        extra = sorted(set(group).difference({"kind", "items"}))
        if extra:
            raise ValueError(
                f"fact_groups[{index}] contains display or undeclared fields: {', '.join(extra)}"
            )
        missing = sorted({"kind", "items"}.difference(group))
        if missing:
            raise ValueError(f"fact_groups[{index}] is missing fields: {', '.join(missing)}")
        kind = _require_code(group.get("kind"), field_name=f"fact_groups[{index}].kind")
        items = group["items"]
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray, Mapping)):
            raise TypeError(f"fact_groups[{index}].items must be a sequence of facts")
        normalized.append(
            {
                "kind": kind,
                "items": _facts(
                    items,
                    field_name=f"fact_groups[{index}].items",
                ),
            }
        )
    return normalized


def _optional_code(value: str | None, *, field_name: str) -> str:
    if value is None:
        return ""
    return _require_code(value, field_name=field_name)


def _optional_fact(value: FactInput | None, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    return _fact(value, field_name=field_name).as_dict()


def _risk_fact(value: FactInput | None) -> dict[str, Any]:
    risk = _optional_fact(value, field_name="risk")
    if not risk:
        return {}
    if "blocks" in risk:
        risk["blocks"] = _facts(risk["blocks"], field_name="risk.blocks")
    return risk


def indicator_fact_payload(
    *,
    scenario: str | None = None,
    setup: str | None = None,
    trigger_event: FactInput | None = None,
    supporting: FactInput | Sequence[FactInput] | None = None,
    opposing: FactInput | Sequence[FactInput] | None = None,
    context: FactInput | Sequence[FactInput] | None = None,
    risk: FactInput | None = None,
    quality: FactInput | None = None,
    fact_groups: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    metrics: Mapping[str, Any] | None = None,
    trade_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize an already typed producer fact bundle without legacy normalization."""

    payload: dict[str, Any] = {}
    scenario_code = _optional_code(scenario, field_name="scenario")
    setup_code = _optional_code(setup, field_name="setup")
    trigger = _optional_fact(trigger_event, field_name="trigger_event")
    if scenario_code:
        payload["scenario"] = scenario_code
    if setup_code:
        payload["setup"] = setup_code
    if trigger:
        payload["trigger_event"] = trigger

    evidence = {
        "supporting": _facts(supporting, field_name="evidence.supporting"),
        "opposing": _facts(opposing, field_name="evidence.opposing"),
        "context": _facts(context, field_name="evidence.context"),
    }
    evidence = {key: value for key, value in evidence.items() if value}
    if evidence:
        payload["evidence"] = evidence

    risk_payload = _risk_fact(risk)
    quality_payload = _optional_fact(quality, field_name="quality")
    groups = _fact_groups(fact_groups)
    if risk_payload:
        payload["risk"] = risk_payload
    if quality_payload:
        payload["quality"] = quality_payload
    if groups:
        payload["fact_groups"] = groups
    if metrics is not None and not isinstance(metrics, Mapping):
        raise TypeError("metrics must be a mapping or None")
    if metrics:
        wire_metrics = _wire_value(metrics, field_name="metrics")
        typed_metrics = {
            key: value for key, value in wire_metrics.items() if value not in (None, "", [], {})
        }
        if typed_metrics:
            payload["metrics"] = typed_metrics
    if trade_plan is not None and not isinstance(trade_plan, Mapping):
        raise TypeError("trade_plan must be a mapping or None")
    if trade_plan:
        typed_plan = _wire_value(trade_plan, field_name="trade_plan")
        if "trigger_event" in typed_plan:
            typed_plan["trigger_event"] = _fact(
                typed_plan["trigger_event"],
                field_name="trade_plan.trigger_event",
            ).as_dict()
        payload["trade_plan"] = typed_plan
    return payload


def validate_indicator_fact_fields(source: Mapping[str, Any]) -> None:
    """Fail closed when a runtime item carries malformed nested indicator facts."""

    if not isinstance(source, Mapping):
        raise TypeError("indicator fact fields must be a mapping")
    if "scenario" in source:
        _require_code(source["scenario"], field_name="scenario")
    if "setup" in source:
        _require_code(source["setup"], field_name="setup")
    if "trigger_event" in source:
        if not isinstance(source["trigger_event"], Mapping):
            raise TypeError("trigger_event must be a canonical fact mapping")
        DomainFact.validate_mapping(source["trigger_event"])
    if "evidence" in source:
        evidence = source["evidence"]
        if not isinstance(evidence, Mapping):
            raise TypeError("evidence must be a mapping")
        extra = sorted(set(evidence).difference(_EVIDENCE_GROUPS))
        if extra:
            raise ValueError(f"evidence contains undeclared groups: {', '.join(extra)}")
        for group in _EVIDENCE_GROUPS:
            if group in evidence:
                values = evidence[group]
                if not isinstance(values, list):
                    raise TypeError(f"evidence.{group} must be a list of fact mappings")
                for index, value in enumerate(values):
                    if not isinstance(value, Mapping):
                        raise TypeError(
                            f"evidence.{group}[{index}] must be a canonical fact mapping"
                        )
                    DomainFact.validate_mapping(value)
    if "risk" in source:
        risk = source["risk"]
        if not isinstance(risk, Mapping):
            raise TypeError("risk must be a canonical fact mapping")
        DomainFact.validate_mapping(risk)
        if "blocks" in risk:
            blocks = risk["blocks"]
            if not isinstance(blocks, list):
                raise TypeError("risk.blocks must be a list of fact mappings")
            for index, value in enumerate(blocks):
                if not isinstance(value, Mapping):
                    raise TypeError(f"risk.blocks[{index}] must be a canonical fact mapping")
                DomainFact.validate_mapping(value)
    if "quality" in source:
        if not isinstance(source["quality"], Mapping):
            raise TypeError("quality must be a canonical fact mapping")
        DomainFact.validate_mapping(source["quality"])
    if "fact_groups" in source:
        groups = source["fact_groups"]
        if not isinstance(groups, list):
            raise TypeError("fact_groups must be a list of canonical fact groups")
        for group_index, group in enumerate(groups):
            if not isinstance(group, Mapping):
                raise TypeError(f"fact_groups[{group_index}] must be a mapping")
            extra = sorted(set(group).difference({"kind", "items"}))
            if extra:
                raise ValueError(
                    f"fact_groups[{group_index}] contains display or undeclared fields: "
                    + ", ".join(extra)
                )
            missing = sorted({"kind", "items"}.difference(group))
            if missing:
                raise ValueError(
                    f"fact_groups[{group_index}] is missing fields: {', '.join(missing)}"
                )
            _require_code(group["kind"], field_name=f"fact_groups[{group_index}].kind")
            items = group["items"]
            if not isinstance(items, list):
                raise TypeError(f"fact_groups[{group_index}].items must be a list of fact mappings")
            for item_index, value in enumerate(items):
                if not isinstance(value, Mapping):
                    raise TypeError(
                        f"fact_groups[{group_index}].items[{item_index}] must be a canonical fact mapping"
                    )
                DomainFact.validate_mapping(value)
    if "metrics" in source:
        if not isinstance(source["metrics"], Mapping):
            raise TypeError("metrics must be a mapping")
        validate_domain_value(source["metrics"], field_name="metrics")
    if "trade_plan" in source:
        if not isinstance(source["trade_plan"], Mapping):
            raise TypeError("trade_plan must be a mapping")
        indicator_fact_payload(trade_plan=source["trade_plan"])


def copy_indicator_facts(source: Mapping[str, Any] | None) -> dict[str, Any]:
    if source is None:
        return {}
    if not isinstance(source, Mapping):
        raise TypeError("indicator facts source must be a mapping or None")
    payload = {
        key: _wire_value(source[key], field_name=key)
        for key in INDICATOR_FACT_FIELD_KEYS
        if key in source
    }
    validate_indicator_fact_fields(payload)
    return payload
