from __future__ import annotations

from tests.source_contracts import python_source_contains


def test_python_source_contains_ignores_layout_but_preserves_tokens() -> None:
    source = 'call(\n    "exact text",\n    option=True,\n)\n'

    assert python_source_contains(source, 'call("exact text", option=True')
    assert not python_source_contains(source, 'call("exact  text", option=True')
    assert not python_source_contains(source, 'call("exact text", option=False')
