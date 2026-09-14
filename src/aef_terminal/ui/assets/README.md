# Browser asset map

Use this map to choose the first browser file for a task instead of scanning the whole asset tree.
System boundaries remain defined by [Architecture](../../../../docs/ARCHITECTURE.md#ui), and the
runtime asset order remains owned by `../asset_services.py` and indicator manifests.

## Boundary

- Core browser interaction, transport projection, geometry, and rendering live here.
- Market, session, signal, alert-evaluation, and execution truth stay on the server.
- Indicator-specific JS and CSS live with the owning package under
  `../../indicators/modules/` and are declared through `ui_js_assets` and `ui_css_assets`.
- The server exposes the assembled `martincall.js`, `martincall.css`, and quote worker; individual
  source files are not public asset routes.

## Assembly and execution

`../asset_services.py` is the canonical assembler. It prepends the generated durable-client-settings
key manifest, quote-row wire manifest, indicator manifest, module catalog, and quote-worker URL,
then concatenates
`JS_ASSET_FILES` into one classic deferred script with shared lexical scope. Indicator JS is
inserted after `49-overlays-indicators.js` in discovery order `(pipeline_order, id)`, preserving
each package's declared asset order.

The assembled quote worker receives that same generated quote-row manifest before its source. The
worker and page quote helpers consume it directly and contain no fallback field registry.

Core CSS follows `CSS_ASSET_FILES`; indicator CSS follows all core CSS. The HTML template receives
content-hashed stylesheet and script tags. The quote worker executes in a separate worker scope.
Numeric filename prefixes are navigation hints only: never infer or change load order by sorting
filenames.

## Start by task

| Task | Start here |
|---|---|
| Asset order, hashing, minification, or compression | `../asset_services.py` |
| Asset HTTP routes and cache headers | `../routers/assets.py` |
| DOM elements and stable element IDs | `templates/index.html` |
| Boot order, page lifecycle, or heartbeat work | `js/70-settings-boot.js` |
| Browser-generated UUIDv4 identity | `js/04-browser-identity.js` |
| Browser state or durable settings | `js/10-core-state.js`, `js/07-settings-reducer.js`, `js/08-server-storage.js`, `js/20-storage-transport.js`, `js/20-settings-sync.js`, `js/20-storage-sync.js` |
| Market loading or realtime projection | `js/20-data-services.js`, `js/20-live-stream-connect.js`, `js/20-live-candle-sync.js` |
| Chart geometry, viewport navigation, and render passes | `js/40-chart-core.js`, `js/59-chart-navigation.js`, `js/52-overlays-price-render.js` |
| Shared indicator UI hosts | `js/30-panels-indicator-controls.js`, `js/48-overlays-manager.js`, `js/49-overlays-indicators.js` |
| One indicator's UI | Its package under `../../indicators/modules/` |
| Drawings, alerts, or Option Points | The relevant `js/59-*` or `js/60-*` file below |
| Shared quote transport | `js/18-stream-sharing.js`, `workers/quote-stream-shared-worker.js` |

## Core JavaScript

### Foundation and state

| File | Owns |
|---|---|
| `00-debug.js` | Debug panel, browser telemetry, and performance-budget harness |
| `04-constants.js` | Shared refresh, reconnect, render, and interaction constants |
| `04-browser-identity.js` | Cryptographic UUIDv4 generation in secure and private-HTTP contexts |
| `04-gex-config.js` | GEX-specific client timing and display enumerations |
| `05-workspace.js` | Workspace slots and popouts, session scope, and initial view values |
| `06-labels.js` | Shared compact label and marker vocabulary |
| `07-settings-reducer.js` | Pure mutation normalization, tuple ordering, and reconciliation plans |
| `07-preferences.js` | Defaults, normalization, and reads for presentation and indicator preferences |
| `08-server-storage.js` | Durable settings queue, persistence acknowledgements, storage side effects, and cross-window transport |
| `09-workspace-state.js` | Workspace writes and scoped or popout URLs |
| `10-core-state.js` | Main browser state and layout, workspace-dock surface lifecycle, identity and theme helpers, and chart-object normalization |
| `11-indicator-settings-state.js` | Manifest-driven Calc, Visible, and control state |
| `12-floating-panels.js` | Floating-panel dragging and viewport clamping |
| `12-ui-feedback.js` | Typed owner-scoped notices, toasts, attention events, and settings-save feedback |
| `15-fetch-json.js` | JSON fetch deduplication, timeout, cache, telemetry, and error normalization |

### Transport and data projection

| File | Owns |
|---|---|
| `16-gex-context-contract.js` | GEX payload/history admission and exact route, revision, and authority checks |
| `16-gex-context-runtime.js` | GEX HTTP/live lifecycle, motion state, polling, and browser projection |
| `17-option-board-runtime.js` | Option Board stream, rendering, and exact-contract selection for Option Points |
| `17-option-drive-runtime.js` | Workspace-docked exact trade-instrument and option selection, live-follow premium ladder, timed ARMED paper LMT actions, and route-scoped option journal projection |
| `17-vitality-health.js` | Presentation-only vitality state and animation |
| `18-browser-capture.js` | Bounded browser chart and terminal capture protocol |
| `18-paper-trading.js` | Paper controls, journal and order projection, chart objects, and interactions |
| `18-stream-sharing.js` | Required SharedWorker quote transport, window dispatcher, read-model ownership, and linked crosshair |
| `19-reference-data.js` | Provider and watchlist loading, qualification UI, and system-health fetch |
| `20-data-services.js` | Market and history loading, snapshot merge, analysis refresh, and client history window |
| `20-live-candle-sync.js` | Chart-frame admission and order, live-candle projection, and analysis refresh scheduling |
| `23-mtf-lens.js` | Isolated secondary-timeframe candle transport, bounded bar and read-only durable-drawing projection, canvas rendering, resize, and lifecycle |
| `20-live-quote-helpers.js` | Screener and quote-row admission, route checks, and cache merge |
| `20-live-stream-connect.js` | Quote and chart WebSocket lifecycle, screener and trend refresh, and reconnect |
| `20-live-streams.js` | Typed quote display selectors, current quote projection, and document title |
| `20-storage-transport.js` | Exact storage scope admission, request cache/deduplication, and HTTP loading |
| `20-settings-sync.js` | Settings projection, boot hydration, and authoritative snapshot reconciliation |
| `20-storage-sync.js` | Route storage orchestration and drawings/alerts reconciliation and persistence |
| `21-gex-chart-controls.js` | GEX context, view, sidebar controls, and chart/dock host selection |
| `22-chart-advisor-mode.js` | Classic or advisor presentation filtering and plan visibility |

### Panels and controls

| File | Owns |
|---|---|
| `30-alert-manager-runtime.js` | Alert Manager bindings and selection actions |
| `30-market-panel-runtime.js` | Market panel, attention and risk presentation, and throttled panel rendering |
| `30-panels-indicator-controls.js` | Indicator registries, controls, lifecycle hooks, settings manager, and extension hosts |
| `30-panels-system-health.js` | System-health dashboard presentation |
| `30-trade-panels-runtime.js` | Typed Trade Setup and execution-authority presentation |
| `30-watchlist-runtime.js` | Watchlist rendering and selection, tabs, trends, and quote flashes |

### Canvas and overlays

| File | Owns |
|---|---|
| `39-tooltip-content.js` | Typed signal, plan, context, and table tooltip composition |
| `39-chart-tooltips.js` | Canvas/DOM tooltip rendering, registration, hit testing, and cursor runtime |
| `40-chart-core.js` | Layout including dock width/side, chart scheduling, exact axes, render-column projection, geometry caches, and render composition |
| `40-chart-drawing-utils.js` | Shared canvas colors, labels, markers, and drawing helpers |
| `41-canvas-structured-primitives.js` | Small typed canvas glyph primitives |
| `47-canvas-manager.js` | Ordered canvas-layer registry |
| `48-overlays-manager.js` | Generic overlay admission, filtering, layering, and manager composition |
| `49-overlays-indicator-labels.js` | Shared indicator label formatting and rendering |
| `49-overlays-indicator-tables.js` | Indicator table renderer registries, shared placement/opacity contract, and Indicator Lens projection |
| `49-overlays-indicators.js` | Generic typed indicator overlays and custom-renderer registration host |
| `50-overlays-gex-history.js` | GEX history lanes |
| `50-overlays-gex-levels.js` | GEX level strength and classification presentation |
| `50-overlays-gex-profile.js` | GEX sidebar and dock profile geometry and rendering using the primary price projection |
| `50-overlays-gex.js` | Shared GEX overlay orchestration, tooltips, and hit behavior |
| `51-economic-calendar.js` | Economic-calendar admission, refresh lifecycle, volume markers, and structured tooltips |
| `52-overlays-price-render.js` | Base candle/volume rendering, adaptive screen-column LOD, and price-overlay passes |
| `58-object-layer-manager.js` | Object-layer draw and hit-test provider registry |
| `59-chart-navigation.js` | Time/price viewport math, bounded multi-page cursor history intent, Go Live, and keyboard navigation |

### Drawings, alerts, and Option Points

| File | Owns |
|---|---|
| `59-option-targets.js` | Option Point CRUD, quote refresh, rendering, and drag interaction |
| `60-chart-context-menu.js` | Chart point lookup and context-menu actions |
| `60-drawing-geometry.js` | Canonical bar-slot and price-to-screen geometry and hit testing |
| `60-drawing-manager.js` | Drawing object manager dialog |
| `60-drawing-storage.js` | Drawing undo and redo and scoped reload |
| `60-drawing-tools.js` | Drawing creation, editing, and rendering tools |
| `60-interactions-drawings-alerts.js` | Pointer/touch dispatch across chart navigation, drawings, alerts, and chart objects |
| `60-price-alerts.js` | Browser alert-rule editing, persistence trigger, and chart projection |
| `65-command-palette.js` | Safe presentation and navigation command palette |

### Settings and boot

| File | Owns |
|---|---|
| `70-settings-actions.js` | Mutating settings and operator actions plus their runtime refresh effects |
| `70-settings-bindings.js` | Main DOM settings bindings |
| `70-settings-gex-bindings.js` | GEX-specific settings bindings |
| `70-settings-boot.js` | Sole boot orchestrator, page lifecycle listeners, and heartbeat |
| `99-post-main.js` | End-of-bundle debug sentinel |

## CSS, template, and worker

| File | Owns |
|---|---|
| `00-theme.css` | Theme tokens, global primitives, and accessibility defaults |
| `10-chart-layout.css` | Main and chart layout, toolbar, frames, and GEX sidebar |
| `12-mtf-lens.css` | Secondary-timeframe Lens frame, canvas layers, resize handles, and TF marker |
| `20-sidebar.css` | Watchlist, indicators, Trade Setup, paper, and sidebar panels |
| `25-option-drive.css` | Shared workspace dock and Indicator Lens layout plus Option Drive contract selector, premium ladder, and compact journal |
| `30-settings.css` | Settings and health controls |
| `40-overlays-alerts.css` | Modals, tooltips, overlays, alerts, Option Board, and command palette |
| `05-ui-shape.css` | Final flat, rounded, and motion override layer |
| `index.html` | Static DOM and element-ID contract |
| `quote-stream-shared-worker.js` | Exact-route quote-socket owner shared by browser windows |

## Change checklist

1. Put terminal-wide browser behavior here; keep one indicator's behavior with its package.
2. Add a core asset at an explicit dependency position in `../asset_services.py`; never use a glob
   or filename sort as the load contract.
3. Preserve `70-settings-boot.js` as the single boot owner.
4. Keep domain truth on the server and pass typed state to shared presentation owners.
5. Update this map when an asset is added, removed, renamed, or changes responsibility.

Focused coverage lives in `tests/test_ui_asset_navigation.py`. Behavior and assembly contracts live
in `tests/test_ui_assets.py`, `tests/test_indicator_architecture.py`,
`tests/test_quote_stream_shared_worker.py`, and package-specific UI tests.
