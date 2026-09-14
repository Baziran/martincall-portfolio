import gzip
import hashlib
import json
import re
from pathlib import Path
from threading import RLock

from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.indicators.module_discovery import (
    indicator_module_asset_paths,
    indicator_module_catalog,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.settings_contract import browser_client_settings_contract_manifest
from aef_terminal.ui.quote_stream_contract import quote_stream_row_contract_manifest


UI_ASSET_DIR = Path(__file__).with_name("assets")
JS_ASSET_FILES = (
    UI_ASSET_DIR / "js" / "00-debug.js",
    UI_ASSET_DIR / "js" / "04-constants.js",
    UI_ASSET_DIR / "js" / "04-browser-identity.js",
    UI_ASSET_DIR / "js" / "04-gex-config.js",
    UI_ASSET_DIR / "js" / "07-settings-reducer.js",
    UI_ASSET_DIR / "js" / "08-server-storage.js",
    UI_ASSET_DIR / "js" / "05-workspace.js",
    UI_ASSET_DIR / "js" / "06-labels.js",
    UI_ASSET_DIR / "js" / "07-preferences.js",
    UI_ASSET_DIR / "js" / "09-workspace-state.js",
    UI_ASSET_DIR / "js" / "10-core-state.js",
    UI_ASSET_DIR / "js" / "12-ui-feedback.js",
    UI_ASSET_DIR / "js" / "12-floating-panels.js",
    UI_ASSET_DIR / "js" / "11-indicator-settings-state.js",
    UI_ASSET_DIR / "js" / "15-fetch-json.js",
    UI_ASSET_DIR / "js" / "18-stream-sharing.js",
    UI_ASSET_DIR / "js" / "16-gex-context-contract.js",
    UI_ASSET_DIR / "js" / "16-gex-context-runtime.js",
    UI_ASSET_DIR / "js" / "18-paper-trading.js",
    UI_ASSET_DIR / "js" / "19-reference-data.js",
    UI_ASSET_DIR / "js" / "20-data-services.js",
    UI_ASSET_DIR / "js" / "17-option-board-runtime.js",
    UI_ASSET_DIR / "js" / "17-option-drive-runtime.js",
    UI_ASSET_DIR / "js" / "20-live-quote-helpers.js",
    UI_ASSET_DIR / "js" / "20-live-stream-connect.js",
    UI_ASSET_DIR / "js" / "18-browser-capture.js",
    UI_ASSET_DIR / "js" / "20-live-streams.js",
    UI_ASSET_DIR / "js" / "20-live-candle-sync.js",
    UI_ASSET_DIR / "js" / "23-mtf-lens.js",
    UI_ASSET_DIR / "js" / "22-chart-advisor-mode.js",
    UI_ASSET_DIR / "js" / "20-storage-transport.js",
    UI_ASSET_DIR / "js" / "20-settings-sync.js",
    UI_ASSET_DIR / "js" / "20-storage-sync.js",
    UI_ASSET_DIR / "js" / "21-gex-chart-controls.js",
    UI_ASSET_DIR / "js" / "30-panels-indicator-controls.js",
    UI_ASSET_DIR / "js" / "30-panels-system-health.js",
    UI_ASSET_DIR / "js" / "30-watchlist-runtime.js",
    UI_ASSET_DIR / "js" / "30-market-panel-runtime.js",
    UI_ASSET_DIR / "js" / "30-trade-panels-runtime.js",
    UI_ASSET_DIR / "js" / "30-alert-manager-runtime.js",
    UI_ASSET_DIR / "js" / "17-vitality-health.js",
    UI_ASSET_DIR / "js" / "39-tooltip-content.js",
    UI_ASSET_DIR / "js" / "39-chart-tooltips.js",
    UI_ASSET_DIR / "js" / "40-chart-core.js",
    UI_ASSET_DIR / "js" / "40-chart-drawing-utils.js",
    UI_ASSET_DIR / "js" / "41-canvas-structured-primitives.js",
    UI_ASSET_DIR / "js" / "47-canvas-manager.js",
    UI_ASSET_DIR / "js" / "48-overlays-manager.js",
    UI_ASSET_DIR / "js" / "49-overlays-indicator-tables.js",
    UI_ASSET_DIR / "js" / "49-overlays-indicator-labels.js",
    UI_ASSET_DIR / "js" / "49-overlays-indicators.js",
    UI_ASSET_DIR / "js" / "50-overlays-gex.js",
    UI_ASSET_DIR / "js" / "50-overlays-gex-levels.js",
    UI_ASSET_DIR / "js" / "50-overlays-gex-history.js",
    UI_ASSET_DIR / "js" / "50-overlays-gex-profile.js",
    UI_ASSET_DIR / "js" / "51-economic-calendar.js",
    UI_ASSET_DIR / "js" / "52-overlays-price-render.js",
    UI_ASSET_DIR / "js" / "58-object-layer-manager.js",
    UI_ASSET_DIR / "js" / "59-option-targets.js",
    UI_ASSET_DIR / "js" / "60-chart-context-menu.js",
    UI_ASSET_DIR / "js" / "60-price-alerts.js",
    UI_ASSET_DIR / "js" / "60-drawing-geometry.js",
    UI_ASSET_DIR / "js" / "60-drawing-storage.js",
    UI_ASSET_DIR / "js" / "60-drawing-tools.js",
    UI_ASSET_DIR / "js" / "60-drawing-manager.js",
    UI_ASSET_DIR / "js" / "59-chart-navigation.js",
    UI_ASSET_DIR / "js" / "60-interactions-drawings-alerts.js",
    UI_ASSET_DIR / "js" / "65-command-palette.js",
    UI_ASSET_DIR / "js" / "70-settings-actions.js",
    UI_ASSET_DIR / "js" / "70-settings-gex-bindings.js",
    UI_ASSET_DIR / "js" / "70-settings-bindings.js",
    UI_ASSET_DIR / "js" / "70-settings-boot.js",
    UI_ASSET_DIR / "js" / "99-post-main.js",
)
CSS_ASSET_FILES = (
    UI_ASSET_DIR / "css" / "00-theme.css",
    UI_ASSET_DIR / "css" / "10-chart-layout.css",
    UI_ASSET_DIR / "css" / "12-mtf-lens.css",
    UI_ASSET_DIR / "css" / "20-sidebar.css",
    UI_ASSET_DIR / "css" / "25-option-drive.css",
    UI_ASSET_DIR / "css" / "30-settings.css",
    UI_ASSET_DIR / "css" / "40-overlays-alerts.css",
    UI_ASSET_DIR / "css" / "05-ui-shape.css",
)
HTML_TEMPLATE_FILE = UI_ASSET_DIR / "templates" / "index.html"
QUOTE_STREAM_WORKER_FILE = UI_ASSET_DIR / "workers" / "quote-stream-shared-worker.js"
PLUGIN_JS_INSERT_AFTER = "49-overlays-indicators.js"

_JS_SOURCE_CACHE: str | None = None
_CSS_SOURCE_CACHE: str | None = None
_QUOTE_STREAM_WORKER_SOURCE_CACHE: str | None = None
_JS_DELIVERY_CACHE: str | None = None
_CSS_DELIVERY_CACHE: str | None = None
_QUOTE_STREAM_WORKER_DELIVERY_CACHE: str | None = None
_JS_GZIP_CACHE: bytes | None = None
_CSS_GZIP_CACHE: bytes | None = None
_QUOTE_STREAM_WORKER_GZIP_CACHE: bytes | None = None
_HTML_SOURCE_CACHE: dict[bool, str] = {}
_JS_BUILD_ID: str | None = None
_CSS_BUILD_ID: str | None = None
_QUOTE_STREAM_WORKER_BUILD_ID: str | None = None
_ASSET_CACHE_LOCK = RLock()


def _strip_debug_panel(html: str) -> str:
    start = "<!-- DEBUG_PANEL_START -->"
    end = "<!-- DEBUG_PANEL_END -->"
    if start not in html or end not in html:
        return html
    before, rest = html.split(start, 1)
    _panel, after = rest.split(end, 1)
    return before + after


def _unwrap_debug_panel_markers(html: str) -> str:
    return html.replace("<!-- DEBUG_PANEL_START -->", "").replace("<!-- DEBUG_PANEL_END -->", "")


def _minify_css_source(source: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    collapsed = re.sub(r"\s+", " ", without_comments)
    # Binary + and - operators inside calc() require surrounding whitespace.
    # Keeping whitespace around + also leaves adjacent-sibling combinators valid.
    collapsed = re.sub(r"\s*([{};,>~])\s*", r"\1", collapsed)
    collapsed = re.sub(r":\s*", ":", collapsed)
    return collapsed.strip()


def _minify_js_source(source: str) -> str:
    """Conservative minify: strip comments and horizontal whitespace, keep newlines for ASI."""
    out: list[str] = []
    index = 0
    length = len(source)
    state = "code"
    while index < length:
        char = source[index]
        next_char = source[index + 1] if index + 1 < length else ""
        if state == "code":
            if char == "/" and next_char == "*":
                state = "block_comment"
                index += 2
                continue
            if char == "'":
                out.append(char)
                state = "single"
                index += 1
                continue
            if char == '"':
                out.append(char)
                state = "double"
                index += 1
                continue
            if char == "`":
                out.append(char)
                state = "template"
                index += 1
                continue
            if char in " \t\r":
                if out and out[-1] not in " \n":
                    out.append(" ")
                index += 1
                continue
            out.append(char)
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                index += 2
                continue
            index += 1
            continue
        if state == "single":
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == "'":
                state = "code"
            index += 1
            continue
        if state == "double":
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == '"':
                state = "code"
            index += 1
            continue
        if state == "template":
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == "`":
                state = "code"
            index += 1
            continue
        index += 1
    compact = "".join(out)
    compact = re.sub(r" +\n", "\n", compact)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    lines = []
    for line in compact.split("\n"):
        if line.lstrip().startswith("//"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"


def _frozen_javascript_manifest(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return (
        "(() => {"
        f"const manifest={encoded};"
        'const freeze=item=>{if(!item||typeof item!=="object"||Object.isFrozen(item))return item;'
        "Object.values(item).forEach(freeze);return Object.freeze(item);};"
        "return freeze(manifest);"
        "})()"
    )


def martincall_js_source_sync() -> str:
    global _JS_SOURCE_CACHE
    if _JS_SOURCE_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _JS_SOURCE_CACHE is None:
                quote_worker_url = (
                    f"/assets/quote-stream-worker.js?v={martincall_quote_worker_build_id_sync()}"
                )
                generated = (
                    "window.CLIENT_SETTINGS_CONTRACT_MANIFEST = "
                    f"{json.dumps(browser_client_settings_contract_manifest(), sort_keys=True, separators=(',', ':'))};"
                    "\nwindow.QUOTE_STREAM_ROW_CONTRACT_MANIFEST = "
                    f"{_frozen_javascript_manifest(quote_stream_row_contract_manifest())};"
                    "\nwindow.INDICATOR_REGISTRY_MANIFEST = "
                    f"{json.dumps(indicator_manifest(), sort_keys=True, separators=(',', ':'))};"
                    "\nwindow.INDICATOR_MODULE_CATALOG = "
                    f"{json.dumps(indicator_module_catalog(), sort_keys=True, separators=(',', ':'))};"
                    "\nwindow.MARTINCALL_QUOTE_WORKER_URL = "
                    f"{json.dumps(quote_worker_url)};"
                )
                asset_files: list[Path] = []
                plugin_assets = indicator_module_asset_paths("js")
                for path in JS_ASSET_FILES:
                    asset_files.append(path)
                    if path.name == PLUGIN_JS_INSERT_AFTER:
                        asset_files.extend(plugin_assets)
                _JS_SOURCE_CACHE = (
                    generated
                    + "\n;\n"
                    + "\n;\n".join(
                        path.read_text(encoding="utf-8").rstrip() for path in asset_files
                    )
                    + "\n"
                )
    assert _JS_SOURCE_CACHE is not None
    return _JS_SOURCE_CACHE


def martincall_js_delivery_sync() -> str:
    global _JS_DELIVERY_CACHE
    if _JS_DELIVERY_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _JS_DELIVERY_CACHE is None:
                _JS_DELIVERY_CACHE = _minify_js_source(martincall_js_source_sync())
    assert _JS_DELIVERY_CACHE is not None
    return _JS_DELIVERY_CACHE


async def martincall_js_delivery_async() -> str:
    if _JS_DELIVERY_CACHE is not None:
        return _JS_DELIVERY_CACHE
    return await run_physical_thread_call(martincall_js_delivery_sync)


def martincall_js_gzip_sync() -> bytes:
    global _JS_GZIP_CACHE
    if _JS_GZIP_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _JS_GZIP_CACHE is None:
                _JS_GZIP_CACHE = gzip.compress(
                    martincall_js_delivery_sync().encode("utf-8"),
                    compresslevel=9,
                    mtime=0,
                )
    assert _JS_GZIP_CACHE is not None
    return _JS_GZIP_CACHE


async def martincall_js_gzip_async() -> bytes:
    if _JS_GZIP_CACHE is not None:
        return _JS_GZIP_CACHE
    return await run_physical_thread_call(martincall_js_gzip_sync)


def martincall_quote_worker_source_sync() -> str:
    global _QUOTE_STREAM_WORKER_SOURCE_CACHE
    if _QUOTE_STREAM_WORKER_SOURCE_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _QUOTE_STREAM_WORKER_SOURCE_CACHE is None:
                worker_source = QUOTE_STREAM_WORKER_FILE.read_text(encoding="utf-8")
                strict_directive = '"use strict";\n'
                if not worker_source.startswith(strict_directive):
                    raise RuntimeError("quote stream worker must start in strict mode")
                generated = (
                    "const QUOTE_STREAM_ROW_CONTRACT_MANIFEST = "
                    f"{_frozen_javascript_manifest(quote_stream_row_contract_manifest())};"
                )
                _QUOTE_STREAM_WORKER_SOURCE_CACHE = (
                    strict_directive
                    + generated
                    + "\n;\n"
                    + worker_source.removeprefix(strict_directive)
                )
    assert _QUOTE_STREAM_WORKER_SOURCE_CACHE is not None
    return _QUOTE_STREAM_WORKER_SOURCE_CACHE


async def martincall_quote_worker_source_async() -> str:
    if _QUOTE_STREAM_WORKER_SOURCE_CACHE is not None:
        return _QUOTE_STREAM_WORKER_SOURCE_CACHE
    return await run_physical_thread_call(martincall_quote_worker_source_sync)


def martincall_quote_worker_delivery_sync() -> str:
    global _QUOTE_STREAM_WORKER_DELIVERY_CACHE
    if _QUOTE_STREAM_WORKER_DELIVERY_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _QUOTE_STREAM_WORKER_DELIVERY_CACHE is None:
                _QUOTE_STREAM_WORKER_DELIVERY_CACHE = _minify_js_source(
                    martincall_quote_worker_source_sync()
                )
    assert _QUOTE_STREAM_WORKER_DELIVERY_CACHE is not None
    return _QUOTE_STREAM_WORKER_DELIVERY_CACHE


async def martincall_quote_worker_delivery_async() -> str:
    if _QUOTE_STREAM_WORKER_DELIVERY_CACHE is not None:
        return _QUOTE_STREAM_WORKER_DELIVERY_CACHE
    return await run_physical_thread_call(martincall_quote_worker_delivery_sync)


def martincall_quote_worker_gzip_sync() -> bytes:
    global _QUOTE_STREAM_WORKER_GZIP_CACHE
    if _QUOTE_STREAM_WORKER_GZIP_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _QUOTE_STREAM_WORKER_GZIP_CACHE is None:
                _QUOTE_STREAM_WORKER_GZIP_CACHE = gzip.compress(
                    martincall_quote_worker_delivery_sync().encode("utf-8"),
                    compresslevel=9,
                    mtime=0,
                )
    assert _QUOTE_STREAM_WORKER_GZIP_CACHE is not None
    return _QUOTE_STREAM_WORKER_GZIP_CACHE


async def martincall_quote_worker_gzip_async() -> bytes:
    if _QUOTE_STREAM_WORKER_GZIP_CACHE is not None:
        return _QUOTE_STREAM_WORKER_GZIP_CACHE
    return await run_physical_thread_call(martincall_quote_worker_gzip_sync)


def martincall_css_source_sync() -> str:
    global _CSS_SOURCE_CACHE
    if _CSS_SOURCE_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _CSS_SOURCE_CACHE is None:
                asset_files = (*CSS_ASSET_FILES, *indicator_module_asset_paths("css"))
                _CSS_SOURCE_CACHE = (
                    "\n".join(path.read_text(encoding="utf-8").rstrip() for path in asset_files)
                    + "\n"
                )
    assert _CSS_SOURCE_CACHE is not None
    return _CSS_SOURCE_CACHE


def martincall_css_delivery_sync() -> str:
    global _CSS_DELIVERY_CACHE
    if _CSS_DELIVERY_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _CSS_DELIVERY_CACHE is None:
                _CSS_DELIVERY_CACHE = _minify_css_source(martincall_css_source_sync())
    assert _CSS_DELIVERY_CACHE is not None
    return _CSS_DELIVERY_CACHE


async def martincall_css_delivery_async() -> str:
    if _CSS_DELIVERY_CACHE is not None:
        return _CSS_DELIVERY_CACHE
    return await run_physical_thread_call(martincall_css_delivery_sync)


def martincall_css_gzip_sync() -> bytes:
    global _CSS_GZIP_CACHE
    if _CSS_GZIP_CACHE is None:
        with _ASSET_CACHE_LOCK:
            if _CSS_GZIP_CACHE is None:
                _CSS_GZIP_CACHE = gzip.compress(
                    martincall_css_delivery_sync().encode("utf-8"),
                    compresslevel=9,
                    mtime=0,
                )
    assert _CSS_GZIP_CACHE is not None
    return _CSS_GZIP_CACHE


async def martincall_css_gzip_async() -> bytes:
    if _CSS_GZIP_CACHE is not None:
        return _CSS_GZIP_CACHE
    return await run_physical_thread_call(martincall_css_gzip_sync)


def martincall_js_build_id_sync() -> str:
    global _JS_BUILD_ID
    if _JS_BUILD_ID is None:
        with _ASSET_CACHE_LOCK:
            if _JS_BUILD_ID is None:
                _JS_BUILD_ID = hashlib.sha1(
                    martincall_js_source_sync().encode("utf-8")
                ).hexdigest()[:10]
    assert _JS_BUILD_ID is not None
    return _JS_BUILD_ID


def martincall_css_build_id_sync() -> str:
    global _CSS_BUILD_ID
    if _CSS_BUILD_ID is None:
        with _ASSET_CACHE_LOCK:
            if _CSS_BUILD_ID is None:
                _CSS_BUILD_ID = hashlib.sha1(
                    martincall_css_source_sync().encode("utf-8")
                ).hexdigest()[:10]
    assert _CSS_BUILD_ID is not None
    return _CSS_BUILD_ID


def martincall_quote_worker_build_id_sync() -> str:
    global _QUOTE_STREAM_WORKER_BUILD_ID
    if _QUOTE_STREAM_WORKER_BUILD_ID is None:
        with _ASSET_CACHE_LOCK:
            if _QUOTE_STREAM_WORKER_BUILD_ID is None:
                _QUOTE_STREAM_WORKER_BUILD_ID = hashlib.sha1(
                    martincall_quote_worker_delivery_sync().encode("utf-8")
                ).hexdigest()[:10]
    assert _QUOTE_STREAM_WORKER_BUILD_ID is not None
    return _QUOTE_STREAM_WORKER_BUILD_ID


def martincall_html_sync(debug: bool = False) -> str:
    if debug not in _HTML_SOURCE_CACHE:
        with _ASSET_CACHE_LOCK:
            if debug not in _HTML_SOURCE_CACHE:
                html = HTML_TEMPLATE_FILE.read_text(encoding="utf-8")
                html = _unwrap_debug_panel_markers(html) if debug else _strip_debug_panel(html)
                html = html.replace("__DEBUG_CLASS__", "debug-mode" if debug else "")
                style_tag = f'  <link rel="stylesheet" href="/assets/martincall.css?v={martincall_css_build_id_sync()}">\n'
                script_tag = f'  <script src="/assets/martincall.js?v={martincall_js_build_id_sync()}" defer></script>\n'
                _HTML_SOURCE_CACHE[debug] = html.replace("</head>", style_tag + "</head>").replace(
                    "</body>",
                    script_tag + "</body>",
                )
    return _HTML_SOURCE_CACHE[debug]


async def martincall_html_async(debug: bool = False) -> str:
    if debug in _HTML_SOURCE_CACHE:
        return _HTML_SOURCE_CACHE[debug]
    return await run_physical_thread_call(martincall_html_sync, debug)
