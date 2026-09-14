from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

from aef_terminal.ui.asset_services import (
    HTML_TEMPLATE_FILE,
    martincall_css_source_sync,
)
from aef_terminal.ui.services import browser_capture


def test_browser_capture_rasterizes_real_terminal_dom_and_canvas_layers() -> None:
    html = HTML_TEMPLATE_FILE.read_text(encoding="utf-8").replace(
        "__DEBUG_CLASS__",
        "",
    )
    html = html.replace(
        "</head>",
        f"<style>{martincall_css_source_sync()}</style></head>",
    )
    capture_source = Path("src/aef_terminal/ui/assets/js/18-browser-capture.js").read_text(
        encoding="utf-8"
    )
    capture_source += (
        "\nwindow.__martincallCaptureTest = {browserCaptureTarget, rasterizeBrowserCaptureTarget};"
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                device_scale_factor=1,
            )
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.set_content(html, wait_until="load")
            page.evaluate(
                """
                () => {
                  for (const canvas of document.querySelectorAll("main canvas")) {
                    const rect = canvas.getBoundingClientRect();
                    canvas.width = Math.max(1, Math.round(rect.width));
                    canvas.height = Math.max(1, Math.round(rect.height));
                    const context = canvas.getContext("2d");
                    context?.clearRect(0, 0, canvas.width, canvas.height);
                  }
                  const persistent = document.getElementById("price-chart");
                  const transient = document.getElementById("price-interaction");
                  if (!persistent || !transient) throw new Error("capture fixture canvases missing");
                  const persistentContext = persistent.getContext("2d");
                  const transientContext = transient.getContext("2d");
                  if (!persistentContext || !transientContext) {
                    throw new Error("capture fixture canvas context missing");
                  }
                  persistentContext.fillStyle = "rgb(220, 20, 30)";
                  persistentContext.fillRect(0, 0, persistent.width, persistent.height);
                  transientContext.fillStyle = "rgb(10, 230, 20)";
                  transientContext.fillRect(0, 0, transient.width, transient.height);
                }
                """
            )
            page.add_script_tag(content=capture_source)
            captures = page.evaluate(
                """
                async () => {
                  async function capture(scope) {
                    const target = window.__martincallCaptureTest.browserCaptureTarget(scope);
                    const rect = target?.getBoundingClientRect();
                    if (!target || !rect) throw new Error(`target unavailable: ${scope}`);
                    const startedAt = performance.now();
                    const result = await Promise.race([
                      window.__martincallCaptureTest.rasterizeBrowserCaptureTarget(target),
                      new Promise((_, reject) => window.setTimeout(
                        () => reject(new Error(`test timeout: ${scope}`)),
                        13_000,
                      )),
                    ]);
                    const elapsedMs = performance.now() - startedAt;
                    const reader = new FileReader();
                    const dataUrl = await new Promise((resolve, reject) => {
                      reader.onload = () => resolve(reader.result);
                      reader.onerror = () => reject(reader.error);
                      reader.readAsDataURL(result.blob);
                    });
                    let sample = null;
                    if (scope === "price") {
                      const image = await createImageBitmap(result.blob);
                      const sampleCanvas = document.createElement("canvas");
                      sampleCanvas.width = image.width;
                      sampleCanvas.height = image.height;
                      const context = sampleCanvas.getContext("2d");
                      context.drawImage(image, 0, 0);
                      const x = Math.min(
                        image.width - 1,
                        Math.max(0, Math.floor(image.width * 0.82)),
                      );
                      const y = Math.min(
                        image.height - 1,
                        Math.max(0, Math.floor(image.height * 0.58)),
                      );
                      sample = Array.from(context.getImageData(x, y, 1, 1).data);
                      image.close();
                    }
                    return {
                      base64: String(dataUrl).split(",", 2)[1],
                      width: result.width,
                      height: result.height,
                      elapsed_ms: elapsedMs,
                      sample,
                      css_width: rect.width,
                      css_height: rect.height,
                    };
                  }
                  return {
                    price: await capture("price"),
                    terminal: await capture("terminal"),
                  };
                }
                """
            )
        finally:
            browser.close()

    for scope in sorted(browser_capture.BROWSER_CAPTURE_SCOPES):
        result = captures[scope]
        png = base64.b64decode(result["base64"], validate=True)
        content, width, height = browser_capture._validated_png(png)
        assert content == png
        assert (width, height) == (result["width"], result["height"])
        assert width > 32
        assert height > 32
        assert result["css_width"] > 32
        assert result["css_height"] > 32
        assert result["elapsed_ms"] < 12_000

    red, green, blue, alpha = captures["price"]["sample"]
    assert red >= 170
    assert green <= 80
    assert blue <= 80
    assert alpha == 255
