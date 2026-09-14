#!/usr/bin/env python3
"""Run the MartinCall browser perf budget harness against a local terminal."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aef_terminal.config import AppConfig


DEFAULT_BUDGETS = {
    "loadMarketP95Ms": 1200,
    "renderChartsP95Ms": 120,
    "fetchMarketP95Ms": 1500,
    "fetchHeadersMarketP95Ms": 1000,
    "fetchBodyMarketP95Ms": 250,
    "fetchJsonParseMarketP95Ms": 80,
    "marketNormalizeCompactP95Ms": 80,
    "marketStateCommitP95Ms": 80,
    "chartRenderQueueP95Ms": 34,
    "chartRenderToPaintP95Ms": 80,
    "liveCandleGatewayToPaintP95Ms": 300,
    "quoteWsParseP95Ms": 4,
    "quoteGatewayToPaintP95Ms": 400,
    "chartWsParseP95Ms": 4,
    "quoteMergeP95Ms": 8,
    "renderWatchlistP95Ms": 8,
    "watchlistRafQueueP95Ms": 20,
    "quoteToCommitP95Ms": 16,
    "quoteToPaintP95Ms": 34,
    "crosshairPointerToCommitP95Ms": 8,
    "crosshairRemoteToCommitP95Ms": 20,
    "animationFrameGapP95Ms": 20,
    "mainThreadLongTaskCount": 0,
    "mainThreadLongTaskMaxMs": 50,
    "marketFetchPerMinute": 18,
    "screenerFetchPerMinute": 18,
    "fetchTotalPerMinute": 80,
    "storageFetchPerMinute": 2,
    "paperFetchPerMinute": 2,
    "paperOrdersFetchPerMinute": 2,
    "paperTradesFetchPerMinute": 2,
    "optionTargetsFetchPerMinute": 2,
    "gexFetchPerMinute": 6,
    "tickFetchPerMinute": 75,
    "marketAnalysisFetchPerMinute": 20,
    "optionPricingFetchPerMinute": 12,
    "marketResponseMaxBytes": 3_000_000,
    "quoteWsPayloadMaxBytes": 250_000,
    "chartWsPayloadMaxBytes": 500_000,
}


def _parse_budget(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("budget must use key=value")
    key, raw = value.split("=", 1)
    key = key.strip()
    if key not in DEFAULT_BUDGETS:
        choices = ", ".join(sorted(DEFAULT_BUDGETS))
        raise argparse.ArgumentTypeError(f"unknown budget {key!r}; expected one of: {choices}")
    try:
        budget = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid numeric budget {raw!r}") from exc
    if budget < 0:
        raise argparse.ArgumentTypeError("budget must be non-negative")
    return key, budget


def _output_path(value: str, *, data_root: Path | None = None) -> Path:
    root = (data_root or AppConfig().data_root).expanduser().resolve()
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(root):
        raise argparse.ArgumentTypeError(f"output must resolve below data root: {root}")
    return path


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open MartinCall and run window.mcStartPerfBudgetHarness for 3-5 minutes.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--instrument-id", required=True)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--range", dest="range_", default="3d")
    parser.add_argument("--slot", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument(
        "--side-tab",
        choices=("instruments", "indicators", "ai", "alerts", "go"),
        default="instruments",
    )
    parser.add_argument("--duration-ms", type=int, default=180_000)
    parser.add_argument("--warmup-ms", type=int, default=15_000)
    parser.add_argument("--tabs", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--distinct-slots",
        action="store_true",
        help="Open tab N on workspace slot N instead of using --slot for every tab.",
    )
    parser.add_argument(
        "--fail-on-browser-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail the budget when the browser reports console errors or uncaught page errors.",
    )
    parser.add_argument(
        "--fail-on-api-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail the budget when an API response returns HTTP 4xx/5xx.",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    parser.add_argument("--output", type=_output_path)
    parser.add_argument("--budget", action="append", type=_parse_budget, default=[])
    return parser.parse_args()


def _target_url(
    base_url: str,
    instrument_id: str,
    timeframe: str,
    range_: str,
    slot: int,
    side_tab: str,
    tab_index: int,
) -> str:
    params = {
        "slot": str(slot),
        "popout": "1",
        "instrument_id": instrument_id,
        "timeframe": timeframe,
        "interval": timeframe,
        "range": range_,
        "sideTab": side_tab,
        "codex_diag": f"perf_budget_{int(time.time())}_{slot}_{tab_index}",
    }
    return f"{base_url.rstrip('/')}?{urlencode(params)}"


def _combined_result(
    results: list[dict[str, Any]],
    browser_errors: list[dict[str, Any]],
    api_errors: list[dict[str, Any]],
    owner_samples: list[dict[str, Any]],
    *,
    fail_on_browser_error: bool,
    fail_on_api_error: bool,
) -> dict[str, Any]:
    checks = []
    for index, result in enumerate(results, start=1):
        for check in result.get("checks", []):
            item = dict(check)
            item["tab"] = index
            checks.append(item)
    if fail_on_browser_error:
        checks.append(
            {
                "key": "browserErrors",
                "actual": len(browser_errors),
                "budget": 0,
                "pass": not browser_errors,
            }
        )
    if fail_on_api_error:
        checks.append(
            {
                "key": "apiErrors",
                "actual": len(api_errors),
                "budget": 0,
                "pass": not api_errors,
            }
        )
    owner_count = sum(1 for sample in owner_samples if sample.get("owns") is True)
    checks.append(
        {
            "key": "backgroundPollingOwners",
            "actual": owner_count,
            "budget": 1,
            "pass": owner_count <= 1
            and all(sample.get("owns") is not None for sample in owner_samples),
        }
    )
    return {
        "ok": all(bool(result.get("ok")) for result in results)
        and (not fail_on_browser_error or not browser_errors)
        and (not fail_on_api_error or not api_errors)
        and all(check.get("pass") for check in checks),
        "tabs": len(results),
        "results": results,
        "browserErrors": browser_errors,
        "apiErrors": api_errors,
        "backgroundPollingOwners": owner_samples,
        "checks": checks,
    }


def _attach_page_diagnostics(
    page: Any,
    tab_index: int,
    browser_errors: list[dict[str, Any]],
    api_errors: list[dict[str, Any]],
) -> None:
    def record(kind: str, text: Any, *, level: str = "") -> None:
        message = " ".join(str(text or "").split())
        if not message:
            return
        browser_errors.append(
            {
                "tab": tab_index,
                "kind": kind,
                "level": level,
                "text": message[:600],
            }
        )

    page.on(
        "console",
        lambda message: (
            record("console", message.text, level=message.type) if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda exception: record("pageerror", exception))

    def record_api_error(response: Any) -> None:
        status = int(response.status)
        url = str(response.url)
        if status < 400 or "/api/" not in url:
            return
        api_errors.append(
            {
                "tab": tab_index,
                "status": status,
                "url": url[:600],
            }
        )

    page.on("response", record_api_error)


def _background_owner_samples(pages: list[Any]) -> list[dict[str, Any]]:
    samples = []
    for index, page in enumerate(pages, start=1):
        sample = page.evaluate(
            """() => ({
                tabVisible: document.visibilityState === "visible",
                clientId: typeof state === "object" && state ? state.clientId || "" : "",
                key: typeof backgroundPollKey === "function" ? backgroundPollKey() : "",
                owns: typeof ownsBackgroundPolling === "function" ? ownsBackgroundPolling() : null
            })"""
        )
        sample["tab"] = index
        samples.append(sample)
    return samples


def _start_pointer_sweeps(pages: list[Any]) -> None:
    period_ms = max(len(pages), 1) * 700
    for index, page in enumerate(pages):
        page.evaluate(
            """config => {
                window.clearTimeout(window.mcPerfPointerStartTimer);
                window.clearInterval(window.mcPerfPointerSweepTimer);
                let step = 0;
                const dispatchMove = () => {
                    const canvas = document.getElementById("price-chart");
                    if (!canvas) return;
                    const rect = canvas.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return;
                    const phase = step % 2 === 0 ? 0.32 : 0.68;
                    step += 1;
                    canvas.dispatchEvent(new MouseEvent("mousemove", {
                        bubbles: true,
                        clientX: rect.left + rect.width * phase,
                        clientY: rect.top + rect.height * (phase === 0.32 ? 0.42 : 0.58),
                    }));
                    if (step % 2 === 0) {
                        canvas.dispatchEvent(new WheelEvent("wheel", {
                            bubbles: true,
                            cancelable: true,
                            clientX: rect.left + rect.width * 0.5,
                            clientY: rect.top + rect.height * 0.5,
                            deltaY: step % 4 === 0 ? 60 : -60,
                        }));
                    }
                };
                window.mcPerfPointerStartTimer = window.setTimeout(() => {
                    dispatchMove();
                    window.mcPerfPointerSweepTimer = window.setInterval(dispatchMove, config.periodMs);
                }, config.delayMs);
            }""",
            {"delayMs": index * 700, "periodMs": period_ms},
        )


def _stop_pointer_sweeps(pages: list[Any]) -> None:
    for page in pages:
        page.evaluate(
            """() => {
                window.clearTimeout(window.mcPerfPointerStartTimer);
                window.clearInterval(window.mcPerfPointerSweepTimer);
                window.mcPerfPointerStartTimer = null;
                window.mcPerfPointerSweepTimer = null;
            }"""
        )


def main() -> int:
    args = _args()
    budgets = dict(DEFAULT_BUDGETS)
    budgets.update(dict(args.budget))
    duration_ms = max(180_000, min(300_000, int(args.duration_ms)))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required. Install project dev dependencies and run "
            "`playwright install chromium` before this harness."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo_ms)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            pages = []
            browser_errors: list[dict[str, Any]] = []
            api_errors: list[dict[str, Any]] = []
            for tab_index in range(1, args.tabs + 1):
                slot = tab_index if args.distinct_slots else args.slot
                page = context.new_page()
                _attach_page_diagnostics(page, tab_index, browser_errors, api_errors)
                page.goto(
                    _target_url(
                        args.base_url,
                        args.instrument_id,
                        args.timeframe,
                        args.range_,
                        slot,
                        args.side_tab,
                        tab_index,
                    ),
                    wait_until="domcontentloaded",
                )
                page.wait_for_function("window.mcStartPerfBudgetHarness", timeout=30_000)
                pages.append(page)

            warmup_ms = max(0, int(args.warmup_ms))
            if warmup_ms:
                pages[0].wait_for_timeout(warmup_ms)
            pages[0].bring_to_front()
            for page_index, page in enumerate(pages):
                page_budgets = dict(budgets)
                if page_index > 0:
                    page_budgets["animationFrameGapP95Ms"] = None
                    page_budgets["mainThreadLongTaskCount"] = None
                    page_budgets["mainThreadLongTaskMaxMs"] = None
                page.evaluate(
                    """config => window.mcStartPerfBudgetHarness(config)""",
                    {
                        "instrumentId": args.instrument_id,
                        "timeframe": args.timeframe,
                        "range": args.range_,
                        "durationMs": duration_ms,
                        "budgets": page_budgets,
                        "requiredSamples": {
                            "quoteWsParseP95Ms": 5,
                            "chartWsParseP95Ms": 5,
                            "liveCandleGatewayToPaintP95Ms": 1 if page_index == 0 else 0,
                            "quoteGatewayToPaintP95Ms": 5 if page_index == 0 else 0,
                            "quoteMergeP95Ms": 1,
                            "renderWatchlistP95Ms": 1,
                            "watchlistRafQueueP95Ms": 1,
                            "quoteToCommitP95Ms": 1,
                            "quoteToPaintP95Ms": 1,
                            "crosshairPointerToCommitP95Ms": 8,
                            "crosshairRemoteToCommitP95Ms": (
                                8 if args.tabs == 2 and not args.distinct_slots else 0
                            ),
                            "animationFrameGapP95Ms": 120 if page_index == 0 else 0,
                            "mainThreadLongTaskCount": 1 if page_index == 0 else 0,
                            "mainThreadLongTaskMaxMs": 1 if page_index == 0 else 0,
                            "quoteWsPayloadMaxBytes": 5,
                            "chartWsPayloadMaxBytes": 5,
                        },
                    },
                )
            _start_pointer_sweeps(pages)
            wait_ms = duration_ms + 5_000
            pages[0].wait_for_timeout(wait_ms)
            _stop_pointer_sweeps(pages)
            owner_samples = _background_owner_samples(pages)
            results = [
                page.evaluate("""() => window.mcFinishPerfBudgetHarness("script")""")
                for page in pages
            ]
            combined = _combined_result(
                results,
                browser_errors,
                api_errors,
                owner_samples,
                fail_on_browser_error=bool(args.fail_on_browser_error),
                fail_on_api_error=bool(args.fail_on_api_error),
            )
        finally:
            browser.close()

    payload = json.dumps(combined, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if combined["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
