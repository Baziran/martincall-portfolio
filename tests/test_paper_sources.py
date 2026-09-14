from aef_terminal.ui.paper.constants import PAPER_ALLOWED_SOURCES, paper_indicator_source_ids


def test_paper_allowed_sources_trade_setup_only() -> None:
    assert PAPER_ALLOWED_SOURCES == frozenset({"trade_setup"})
    assert "linda_volume" not in PAPER_ALLOWED_SOURCES


def test_paper_indicator_source_ids_cover_actionable_registry() -> None:
    sources = paper_indicator_source_ids()
    assert "linda_volume" in sources
    assert "impulse_fib" in sources
    assert "smc_structure" in sources
    assert "vsa_volume" not in sources
    assert "elliott_core" not in sources
    assert "martin_carlo" not in sources
