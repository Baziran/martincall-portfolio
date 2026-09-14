from __future__ import annotations

import pytest

from aef_terminal.ui.paper.config import paper_config


def test_paper_config_accepts_only_exact_canonical_policy() -> None:
    assert paper_config({"min_rr": "1.25", "edge_gate": False}) == {
        "min_rr": 1.25,
        "edge_gate": False,
    }

    for invalid in (
        None,
        {"min_rr": 1.0, "edge_gate": True},
        {"min_rr": 1.25, "edge_gate": "false"},
        {"min_rr": 1.25, "edge_gate": True, "execution_plan_gate": False},
        {"min_rr": 1.25, "edge_gate": True, "invert_mode": "wolfe_w5"},
    ):
        with pytest.raises(ValueError):
            paper_config(invalid)
