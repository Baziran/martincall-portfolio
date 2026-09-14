from __future__ import annotations

from typing import Any

from aef_terminal.ui.paper.constants import PAPER_ALLOWED_MIN_RR


def paper_min_rr(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("PAPER_MIN_RR_INVALID")
    try:
        raw = float(value)
    except TypeError, ValueError:
        raise ValueError("PAPER_MIN_RR_INVALID") from None
    if raw not in PAPER_ALLOWED_MIN_RR:
        raise ValueError("PAPER_MIN_RR_INVALID")
    return raw


def paper_config(
    raw: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"min_rr", "edge_gate"}:
        raise ValueError("PAPER_CONFIG_FIELDS_INVALID")
    edge_gate = raw["edge_gate"]
    if not isinstance(edge_gate, bool):
        raise ValueError("PAPER_EDGE_GATE_INVALID")
    return {
        "min_rr": paper_min_rr(raw["min_rr"]),
        "edge_gate": edge_gate,
    }
