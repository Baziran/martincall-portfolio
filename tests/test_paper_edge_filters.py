from aef_terminal.ui.paper.edge_filters import paper_edge_context, paper_normalize_edge_rule
from aef_terminal.ui.paper.signals import paper_signal_rejection


def test_paper_edge_rule_requires_explicit_utc_daypart_contract() -> None:
    legacy = {
        "instrument_id": "instrument-a",
        "source": "impulse_pullback",
        "side": "long",
        "session": "new_york",
        "action": "block",
    }
    assert paper_normalize_edge_rule(legacy) is None

    current = {
        **legacy,
        "utc_daypart": "utc_13_21",
    }
    current.pop("session")
    assert paper_normalize_edge_rule(current)["utc_daypart"] == "utc_13_21"


def test_paper_edge_context_fails_closed_when_signal_time_is_unknown() -> None:
    context = paper_edge_context(
        "instrument-a",
        {"signal_source": "impulse_pullback", "ts": ""},
        "long",
        edge_filter_loader=lambda: [],
    )

    assert context["utc_daypart"] == "unknown"
    assert context["action"] == "block"


def test_research_edge_artifacts_never_gate_paper_entry() -> None:
    rejection = paper_signal_rejection(
        {
            "source": "trade_setup",
            "signal_source": "impulse_pullback",
            "action": "GO",
            "side": "long",
            "ts": "2026-08-23T15:00:00+00:00",
        },
        {"kind": "transit"},
    )

    assert rejection == ""
