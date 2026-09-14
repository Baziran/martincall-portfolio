from collections.abc import Mapping
from types import MappingProxyType

import pytest

from aef_terminal.domain import (
    DomainFact,
    domain_frozen_value,
    domain_wire_value,
    validate_domain_value,
)
from aef_terminal.indicators.domain_facts import validate_indicator_fact_fields


def test_domain_value_freeze_and_wire_preserve_deep_isolation_and_fact_projection() -> None:
    source = {"levels": [{"price": 102.5}], "active": True}
    nested = DomainFact("confirmed", {"rows": source["levels"]})
    fact = DomainFact("breakout", {"context": source, "evidence": nested})
    source["levels"][0]["price"] = 999.0
    first = fact.as_dict()
    assert first == {
        "code": "breakout",
        "context": {"levels": [{"price": 102.5}], "active": True},
        "evidence": {"code": "confirmed", "rows": [{"price": 102.5}]},
    }
    first["evidence"]["rows"][0]["price"] = -1.0
    assert fact.as_dict()["evidence"]["rows"][0]["price"] == 102.5
    with pytest.raises(TypeError):
        fact.attributes["context"]["levels"][0]["price"] = 0
    frozen = domain_frozen_value(fact, field_name="root")
    assert isinstance(frozen, MappingProxyType)
    assert domain_wire_value(frozen, field_name="root") == fact.as_dict()


@pytest.mark.parametrize("copier", [domain_frozen_value, domain_wire_value, validate_domain_value])
@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"x": [float("nan")]}, ValueError),
        ({"x": [float("inf")]}, ValueError),
        ({"x": -float("inf")}, ValueError),
        ({"x": {1: "bad"}}, TypeError),
        ({"x": {"": "bad"}}, TypeError),
        ({"x": b"bad"}, TypeError),
        ({"x": {"a", "b"}}, TypeError),
        ({"x": object()}, TypeError),
    ],
)
def test_domain_copiers_keep_strict_recursive_validation(copier, value, error) -> None:
    with pytest.raises(error, match="root.x"):
        copier(value, field_name="root")


def test_domain_fact_visits_each_mutable_nested_mapping_once() -> None:
    class ObservedMapping(Mapping):
        def __init__(self, items):
            self.data = items
            self.reads = 0

        def __iter__(self):
            return iter(self.data)

        def __len__(self):
            return len(self.data)

        def __getitem__(self, key):
            self.reads += 1
            return self.data[key]

    child = ObservedMapping({"score": 0.8, "confirmed": True})
    parent = ObservedMapping({"items": [child]})
    fact = DomainFact.from_mapping({"code": "ready", "context": parent})
    assert fact.as_dict()["context"]["items"][0]["score"] == 0.8
    assert parent.reads == 1
    assert child.reads == 2


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"code": 1},
        {"code": "UPPER"},
        {"code": " spaced "},
        {"code": "ok", "tooltip": "display"},
        {"code": "ok", "": 1},
        {"code": "ok", 1: 2},
        {"code": "ok", "context": {"rows": [float("nan")]}},
        {"code": "ok", "context": {"rows": [{1: 2}]}},
        {"code": "ok", "context": b"bytes"},
        {"code": "ok", "context": {1, 2}},
    ],
)
def test_fact_validation_preserves_constructor_errors(value) -> None:
    with pytest.raises((TypeError, ValueError)) as constructed:
        DomainFact.from_mapping(value)
    with pytest.raises(type(constructed.value)) as validated:
        DomainFact.validate_mapping(value)
    assert str(validated.value) == str(constructed.value)


def test_indicator_fact_validation_does_not_construct_discarded_frozen_facts(monkeypatch) -> None:
    fact = {"code": "confirmed", "context": {"rows": [{"score": 0.8}]}}
    payload = {
        "trigger_event": fact,
        "evidence": {"supporting": [fact]},
        "risk": {"code": "bounded", "blocks": [fact]},
        "quality": fact,
        "fact_groups": [{"kind": "confirmation", "items": [fact]}],
        "metrics": {"nested": [fact]},
    }

    def unexpected_construction(*_args, **_kwargs):
        raise AssertionError("validation must not allocate DomainFact")

    monkeypatch.setattr(DomainFact, "__init__", unexpected_construction)
    validate_indicator_fact_fields(payload)
    assert fact["context"]["rows"] == [{"score": 0.8}]
