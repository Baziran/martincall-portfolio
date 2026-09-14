from aef_terminal.ui.paper.ranking import (
    paper_flip_rank_allows,
    paper_signal_reliability_rank,
    paper_signal_source_key,
)


def test_paper_signal_reliability_rank_uses_calibrated_weights() -> None:
    assert paper_signal_reliability_rank("impulse_pullback") > paper_signal_reliability_rank(
        "wolfe_confirmed"
    )
    assert paper_signal_reliability_rank("trade_setup") == 0.0


def test_paper_flip_rank_allows_only_higher_rank_source() -> None:
    assert (
        paper_flip_rank_allows(current_source="wolfe_confirmed", new_source="smc_structure") is True
    )
    assert (
        paper_flip_rank_allows(current_source="smc_structure", new_source="wolfe_confirmed")
        is False
    )
    assert (
        paper_flip_rank_allows(current_source="wolfe_confirmed", new_source="wolfe_confirmed")
        is False
    )


def test_paper_signal_source_key_has_one_nested_payload_contract() -> None:
    assert (
        paper_signal_source_key(
            {
                "source": "trade_setup",
                "payload": {
                    "signal_source": "decision",
                    "confluence_sources": [{"source": "Impulse Pullback"}],
                },
            }
        )
        == "impulse_pullback"
    )
