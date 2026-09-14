import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_perf_budget import _output_path
from tests.source_contracts import python_source_contains


def test_perf_budget_harness_uses_browser_runtime_contract() -> None:
    source = Path("scripts/run_perf_budget.py").read_text()
    pyproject = Path("pyproject.toml").read_text()

    assert "playwright>=1.61.0" in pyproject
    assert "window.mcStartPerfBudgetHarness" in source
    assert "window.mcFinishPerfBudgetHarness" in source
    assert '"--instrument-id"' in source
    assert '"--timeframe", default="5m"' in source
    assert '"--range", dest="range_", default="3d"' in source
    assert '"--slot", type=int, choices=(1, 2, 3, 4), default=2' in source
    assert python_source_contains(
        source,
        '"--side-tab", choices=("instruments", "indicators", "ai", "alerts", "go"), default="instruments"',
    )
    assert '"--warmup-ms", type=int, default=15_000' in source
    assert '"--tabs", type=int, choices=(1, 2), default=2' in source
    assert '"--distinct-slots"' in source
    assert '"--fail-on-browser-error"' in source
    assert '"--fail-on-api-error"' in source
    assert "slot = tab_index if args.distinct_slots else args.slot" in source
    assert "api_errors: list[dict[str, Any]] = []" in source
    assert "_attach_page_diagnostics(page, tab_index, browser_errors, api_errors)" in source
    assert python_source_contains(source, 'page.on("console"')
    assert 'page.on("pageerror"' in source
    assert 'page.on("response", record_api_error)' in source
    assert '"/api/" not in url' in source
    assert "pages[0].wait_for_timeout(warmup_ms)" in source
    assert "owner_samples = _background_owner_samples(pages)" in source
    assert '"key": "backgroundPollingOwners"' in source
    assert "owner_count <= 1" in source
    assert '"sideTab": side_tab' in source
    assert '"popout": "1"' in source
    assert '"marketFetchPerMinute": 18' in source
    assert '"storageFetchPerMinute": 2' in source
    assert '"paperOrdersFetchPerMinute": 2' in source
    assert '"paperTradesFetchPerMinute": 2' in source
    assert '"gexFetchPerMinute": 6' in source
    assert '"tickFetchPerMinute": 75' in source
    assert '"marketAnalysisFetchPerMinute": 20' in source
    assert '"manualChannelsFetchPerMinute"' not in source
    assert '"optionPricingFetchPerMinute": 12' in source
    assert '"marketResponseMaxBytes": 3_000_000' in source
    assert '"quoteWsParseP95Ms": 4' in source
    assert '"chartWsParseP95Ms": 4' in source
    assert '"quoteMergeP95Ms": 8' in source
    assert '"renderWatchlistP95Ms": 8' in source
    assert '"watchlistRafQueueP95Ms": 20' in source
    assert '"quoteToCommitP95Ms": 16' in source
    assert '"quoteToPaintP95Ms": 34' in source
    assert '"fetchHeadersMarketP95Ms": 1000' in source
    assert '"fetchBodyMarketP95Ms": 250' in source
    assert '"fetchJsonParseMarketP95Ms": 80' in source
    assert '"marketNormalizeCompactP95Ms": 80' in source
    assert '"marketStateCommitP95Ms": 80' in source
    assert '"chartRenderQueueP95Ms": 34' in source
    assert '"chartRenderToPaintP95Ms": 80' in source
    assert '"crosshairPointerToCommitP95Ms": 8' in source
    assert '"crosshairRemoteToCommitP95Ms": 20' in source
    assert '"animationFrameGapP95Ms": 20' in source
    assert '"mainThreadLongTaskCount": 0' in source
    assert '"mainThreadLongTaskMaxMs": 50' in source
    assert '"quoteWsPayloadMaxBytes": 250_000' in source
    assert '"chartWsPayloadMaxBytes": 500_000' in source
    assert '"chartWsParseP95Ms": 5' in source
    assert '"chartWsPayloadMaxBytes": 5' in source
    assert "def _start_pointer_sweeps(pages: list[Any])" in source
    assert "def _stop_pointer_sweeps(pages: list[Any])" in source
    assert 'new MouseEvent("mousemove"' in source
    assert 'new WheelEvent("wheel"' in source
    assert '"crosshairPointerToCommitP95Ms": 8' in source
    assert "8 if args.tabs == 2 and not args.distinct_slots else 0" in source
    assert "_start_pointer_sweeps(pages)" in source
    assert "_stop_pointer_sweeps(pages)" in source
    assert "pages[0].bring_to_front()" in source
    assert 'page_budgets["animationFrameGapP95Ms"] = None' in source
    assert 'page_budgets["mainThreadLongTaskCount"] = None' in source
    assert 'page_budgets["mainThreadLongTaskMaxMs"] = None' in source
    assert '"animationFrameGapP95Ms": 120 if page_index == 0 else 0' in source
    assert "max(180_000, min(300_000, int(args.duration_ms)))" in source
    assert '"durationMs": duration_ms' in source
    assert "wait_ms = duration_ms + 5_000" in source
    assert '"browserErrors": browser_errors' in source
    assert '"apiErrors": api_errors' in source
    assert '"backgroundPollingOwners": owner_samples' in source
    assert '"key": "browserErrors"' in source
    assert '"key": "apiErrors"' in source
    assert 'return 0 if combined["ok"] else 1' in source


def test_perf_budget_harness_cli_is_parseable() -> None:
    script = Path("scripts/run_perf_budget.py")

    compile(script.read_text(encoding="utf-8"), str(script), "exec")
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--fail-on-api-error" in result.stdout
    assert "--budget" in result.stdout


def test_perf_budget_output_is_confined_to_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert (
        _output_path(
            str(data_root / "performance" / "result.json"),
            data_root=data_root,
        )
        == data_root / "performance" / "result.json"
    )
    with pytest.raises(argparse.ArgumentTypeError, match="below data root"):
        _output_path(str(tmp_path / "result.json"), data_root=data_root)
