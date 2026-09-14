from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from aef_terminal.ui import asset_services as ui_asset_services


def martincall_css_source() -> str:
    return ui_asset_services.martincall_css_source_sync()


def martincall_html(debug: bool = False) -> str:
    return asyncio.run(ui_asset_services.martincall_html_async(debug=debug))


def test_mtf_lens_assets_expose_isolated_two_canvas_contract() -> None:
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    watchlist_source = Path("src/aef_terminal/ui/assets/js/30-watchlist-runtime.js").read_text(
        encoding="utf-8"
    )
    reference_source = Path("src/aef_terminal/ui/assets/js/19-reference-data.js").read_text(
        encoding="utf-8"
    )
    settings_boot_source = Path("src/aef_terminal/ui/assets/js/70-settings-boot.js").read_text(
        encoding="utf-8"
    )
    html = martincall_html()
    css = martincall_css_source()
    js_names = [path.name for path in ui_asset_services.JS_ASSET_FILES]
    css_names = [path.name for path in ui_asset_services.CSS_ASSET_FILES]

    assert js_names.index("20-live-candle-sync.js") < js_names.index("23-mtf-lens.js")
    assert js_names.index("23-mtf-lens.js") < js_names.index("30-watchlist-runtime.js")
    assert css_names.index("10-chart-layout.css") < css_names.index("12-mtf-lens.css")
    assert 'id="mtf-lens-chart" role="img"' in html
    assert 'id="mtf-lens-interaction" aria-hidden="true"' in html
    assert 'id="mtf-lens-resize"' in html
    assert ".tool-button.mtf-secondary-timeframe" in css
    assert '.mtf-lens[data-anchor="bottom-left"]' in css
    assert '.mtf-lens[data-anchor="top-right"]' in css
    assert '.mtf-lens[data-anchor="bottom-left"] .mtf-lens-header' in css
    assert "padding-right: 23px" in css
    assert '.mtf-lens[data-anchor="bottom-right"] .mtf-lens-header' in css
    assert "padding-left: 23px" in css

    assert "consumer_role: scope.consumerRole" in lens_source
    assert "tail_bars: String(scope.tailBars)" in lens_source
    assert 'range: "1d"' in lens_source
    assert "range: scope.range" in lens_source
    assert "const MTF_LENS_CANVAS_PIXEL_BUDGET = 10000000" in lens_source
    assert "Math.sqrt(MTF_LENS_CANVAS_PIXEL_BUDGET / aggregateCssPixels)" in lens_source
    assert "function mtfLensApplyStoredPresentation()" in lens_source
    assert "mtfLensApplyStoredPresentation();" in settings_boot_source
    apply_settings_body = settings_boot_source.split("function applySettings()", 1)[1]
    assert apply_settings_body.index("refreshThemeColorCache();") < apply_settings_body.index(
        "mtfLensApplyStoredPresentation();"
    )
    assert "new WebSocket(`${protocol}//${window.location.host}/ws/chart?" in lens_source
    assert "new ResizeObserver(() => mtfLensConstrainFrame())" in lens_source
    assert "setServerSettingValue(MTF_LENS_WIDTH_SETTING" in lens_source
    assert "setServerSettingValue(MTF_LENS_HEIGHT_SETTING" in lens_source
    assert 'window.addEventListener("pagehide", () => mtfLensClose())' in lens_source
    assert "mtfLensRuntime.reconnectTimer || mtfLensRuntime.reconnectBlocked" in lens_source
    assert "function mtfLensStatusIsTerminal(message)" in lens_source
    assert "state.snapshot" not in lens_source
    assert "state.transport.chart" not in lens_source
    assert "view." not in lens_source

    timeframe_body = watchlist_source.split("function renderTimeframes()", 1)[1]
    assert 'button.addEventListener("contextmenu"' in timeframe_body
    assert (
        'if (typeof mtfLensToggle === "function") mtfLensToggle(nextTimeframe);' in timeframe_body
    )
    assert 'if (typeof mtfLensHandlePrimaryTimeframeChange === "function")' in timeframe_body
    contextmenu_body = timeframe_body.split('button.addEventListener("contextmenu"', 1)[1].split(
        'button.addEventListener("click"', 1
    )[0]
    assert contextmenu_body.index("event.preventDefault();") < contextmenu_body.index(
        "if (nextTimeframe === state.timeframe) return;"
    )
    instrument_body = watchlist_source.split(
        "function switchWatchlistInstrument(nextInstrumentId)", 1
    )[1].split("function setupInstrumentList", 1)[0]
    assert instrument_body.index("mtfLensPrepareForPrimaryScopeChange();") < instrument_body.index(
        "state.instrumentId ="
    )
    assert instrument_body.index("state.instrumentId =") < instrument_body.index(
        "mtfLensRebindCurrentScope();"
    )
    load_instruments_body = reference_source.split(
        "async function loadInstruments(options = {})", 1
    )[1].split("async function addWatchlistInstrumentFromForm", 1)[0]
    assert "const plannedRouteFingerprint" in load_instruments_body
    assert "const selectedRouteScopeChanged" in load_instruments_body
    assert load_instruments_body.index("mtfLensPrepareForPrimaryScopeChange();") < (
        load_instruments_body.index("state.instruments = nextInstruments;")
    )
    assert load_instruments_body.index(
        "if (selected) state.dataSource ="
    ) < load_instruments_body.index("mtfLensRebindCurrentScope();")
    assert load_instruments_body.index("mtfLensRebindCurrentScope();") < (
        load_instruments_body.index("await stopGexLiveContext")
    )


def test_isolated_server_storage_keeps_primary_storage_status_untouched(
    tmp_path: Path,
) -> None:
    storage_source = Path("src/aef_terminal/ui/assets/js/20-storage-transport.js").read_text(
        encoding="utf-8"
    )
    script_path = tmp_path / "isolated-server-storage.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const requests = []; const notices = []; let response;",
                "const state = { instrumentId: 'instrument-a', timeframe: '5m', storageStatus: { ok: true, message: 'primary-ready' } };",
                "function exactIdentityText(value) { return typeof value === 'string' ? value : ''; }",
                "function instrumentRouteFingerprint() { return 'route-a'; }",
                "function instrumentForId(value) { return value === 'instrument-a' ? { provider: 'ibkr', session_contract_id: 'contract-a' } : null; }",
                "function normalizeDrawingObjects(rows) { if (!Array.isArray(rows)) throw new TypeError('malformed drawings'); return rows.map(item => ({ ...item })); }",
                "function normalizePriceAlert(item) { return { ...item }; }",
                "function apiErrorMessage(payload, fallback) { return payload?.message || fallback; }",
                "function normalizeErrorPayload(payload) { return payload; }",
                "function requestErrorMessage(error, fallback) { return error?.message || fallback; }",
                "function setUiNotice(...args) { notices.push(['set', ...args]); }",
                "function clearUiNotice(...args) { notices.push(['clear', ...args]); }",
                "function setScreenerStateLabel() { notices.push(['label']); }",
                "async function fetchJson(url, options) { requests.push({ url, options }); return response; }",
                storage_source,
                "(async () => {",
                "  response = { ok: false, message: 'lens-only failure' };",
                "  await assert.rejects(() => fetchServerStorage('instrument-a', '1m', { settings: false, alerts: false, expectedRouteFingerprint: 'route-a', isolated: true, force: true, reason: 'mtf_lens_drawings' }), /lens-only failure/);",
                "  assert.deepEqual(state.storageStatus, { ok: true, message: 'primary-ready' }, 'isolated failure must not publish primary storage status');",
                "  assert.deepEqual(notices, [], 'isolated failure must not publish or clear the primary storage notice');",
                "  assert.equal(requests.length, 1);",
                "  const query = new URL(requests[0].url, 'http://terminal.test').searchParams;",
                "  assert.equal(query.get('instrument_id'), 'instrument-a'); assert.equal(query.get('route_fingerprint'), 'route-a'); assert.equal(query.get('interval'), '1m');",
                "  assert.equal(query.get('settings'), 'false'); assert.equal(query.get('alerts'), 'false'); assert.equal(query.has('drawings'), false, 'Lens must request exact drawings');",
                "  response = { ok: true, instrument_id: 'instrument-a', route_fingerprint: 'route-a', interval: '1m', provider: 'ibkr', provider_contract_id: 'contract-a', settings: {}, settings_mutations: [], settings_mutation_changed_at_ms: 0, settings_revision: 0, drawings: [], alerts: [] };",
                "  const admitted = await fetchServerStorage('instrument-a', '1m', { settings: false, alerts: false, expectedRouteFingerprint: 'route-a', isolated: true, force: true, reason: 'mtf_lens_drawings' });",
                "  assert.deepEqual(admitted.drawings, [], 'valid empty exact response must remain authoritative');",
                "  assert.deepEqual(state.storageStatus, { ok: true, message: 'primary-ready' }, 'isolated success must not clear or replace primary status');",
                "  assert.deepEqual(notices, [], 'isolated success must not clear a primary notice');",
                "  response = { ok: false, message: 'primary failure' };",
                "  await assert.rejects(() => fetchServerStorage('instrument-a', '1m', { settings: false, alerts: false, expectedRouteFingerprint: 'route-a', force: true, reason: 'primary_objects' }), /primary failure/);",
                "  assert.equal(state.storageStatus.ok, false, 'ordinary storage owner must preserve existing primary failure behavior');",
                "  assert.ok(notices.some(item => item[0] === 'set') && notices.some(item => item[0] === 'label'));",
                "})().catch(error => { console.error(error); process.exitCode = 1; });",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_drawings_use_explicit_read_only_shared_contract() -> None:
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    drawing_source = Path("src/aef_terminal/ui/assets/js/60-drawing-tools.js").read_text(
        encoding="utf-8"
    )
    html = martincall_html()
    lens_html = html.split('id="mtf-lens"', 1)[1].split("</section>", 1)[0]

    assert lens_html.count("<canvas") == 2
    assert 'id="mtf-lens-chart"' in lens_html
    assert 'id="mtf-lens-interaction"' in lens_html
    assert "state.drawing" not in lens_source
    assert "persistDrawingsToServer" not in lens_source
    assert "saveDrawings(" not in lens_source
    assert "hitTestDrawing" not in lens_source
    for field in (
        "canonicalRevision",
        "barsVersion",
        "drawings",
        "drawingsHydrated",
        "drawingExactRefreshPending",
        "drawingRequest",
        "drawingRequestEpoch",
        "drawingRefreshTimer",
        "drawingRefreshAttempts",
        "drawingRefreshScopeKey",
    ):
        assert field in lens_source
    for helper in (
        "mtfLensDrawingSnapshot",
        "mtfLensDrawingsExpectedScope",
        "mtfLensResolveDrawings",
        "mtfLensLoadDrawings",
        "mtfLensScheduleDrawingProjectionRefresh",
        "mtfLensCancelDrawingRuntime",
        "mtfLensHandleSharedObjectsStorageEvent",
    ):
        assert f"function {helper}" in lens_source

    load_body = lens_source.split("function mtfLensLoadDrawings", 1)[1].split(
        "function mtfLensHandleSharedObjectsStorageEvent", 1
    )[0]
    assert "fetchServerStorage(" in load_body
    assert "settings: false" in load_body
    assert "alerts: false" in load_body
    assert "isolated: true" in load_body
    assert "expectedRouteFingerprint: scope.routeFingerprint" in load_body
    assert "signal:" in load_body
    assert "state.drawing" not in load_body

    resolve_body = lens_source.split("function mtfLensResolveDrawings", 1)[1].split(
        "function mtfLensLoadDrawings", 1
    )[0]
    assert "resolveDrawingAnchorsAgainstSnapshot(" in resolve_body
    assert "expectedScope:" in resolve_body
    assert "onProjectionRefreshRequired:" in resolve_body
    render_base_body = lens_source.split("function mtfLensRenderBase()", 1)[1].split(
        "function mtfLensRenderInteraction", 1
    )[0]
    render_interaction_body = lens_source.split("function mtfLensRenderInteraction", 1)[1].split(
        "function mtfLensScheduleRender", 1
    )[0]
    assert "drawDrawingObjects(" not in render_base_body
    assert "mtfLensScheduleInteractionRender();" in render_base_body
    assert "drawDrawingObjects(" in render_interaction_body
    assert "selectedId: null" in render_interaction_body
    assert "showHandles: false" in render_interaction_body
    assert "decorators: false" in render_interaction_body
    assert "rightGapBars: 0" in render_interaction_body
    assert render_interaction_body.index("drawDrawingObjects(") < render_interaction_body.index(
        "mtfLensRuntime.crosshair.visible"
    )

    bind_body = lens_source.split("function mtfLensBindScope(scope)", 1)[1].split(
        "function mtfLensRebindCurrentScope", 1
    )[0]
    prepare_body = lens_source.split("function mtfLensPrepareForPrimaryScopeChange()", 1)[1].split(
        "function mtfLensHandlePrimaryTimeframeChange", 1
    )[0]
    sleep_body = lens_source.split("function mtfLensHandleServerSleepState()", 1)[1].split(
        "function mtfLensSyncTimeframeButtons", 1
    )[0]
    close_body = lens_source.split("function mtfLensClose()", 1)[1].split(
        "function mtfLensApplyPendingResize", 1
    )[0]
    for lifecycle_body in (bind_body, prepare_body, close_body):
        assert "mtfLensCancelDrawingRuntime({ clear: true });" in lifecycle_body
    assert bind_body.index("mtfLensCancelDrawingRuntime({ clear: true });") < bind_body.index(
        "mtfLensRuntime.scope = scope;"
    )
    assert "mtfLensLoadDrawings(scope, mtfLensRuntime.epoch" in bind_body
    assert "mtfLensCancelDrawingRuntime({ clear: true });" in sleep_body
    assert 'reason: "mtf_lens_wake"' in sleep_body

    assert "function drawDrawingObjects(" in drawing_source
    primary_wrapper = drawing_source.split("function drawDrawings(", 1)[1].split(
        "function drawDrawingInteraction", 1
    )[0]
    assert "drawDrawingObjects(" in primary_wrapper


def test_shared_drawing_renderer_uses_explicit_lens_context_and_preserves_primary_wrapper(
    tmp_path: Path,
) -> None:
    drawing_source = Path("src/aef_terminal/ui/assets/js/60-drawing-tools.js").read_text(
        encoding="utf-8"
    )
    shared_source = (
        "function drawDrawingObjects("
        + drawing_source.split("function drawDrawingObjects(", 1)[1].split(
            "function drawDrawings(", 1
        )[0]
    )
    primary_source = (
        "function drawDrawings("
        + drawing_source.split("function drawDrawings(", 1)[1].split(
            "function drawDrawingInteraction", 1
        )[0]
    )
    script_path = tmp_path / "explicit-drawing-render-context.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const primaryObject = { id: 'primary', geometryIntegrity: { status: 'valid' } };",
                "const secondaryObject = { id: 'secondary', geometryIntegrity: { status: 'valid' } };",
                "const syncingObject = { id: 'syncing', geometryIntegrity: { status: 'syncing' } };",
                "const primarySnapshot = { meta: { timeframe: '5m' } }; const secondarySnapshot = { meta: { timeframe: '1m' } };",
                "const state = { timeframe: '5m', snapshot: primarySnapshot, drawing: { hidden: false, objects: [primaryObject], selectedId: 'primary' } };",
                "const chartRenderVersions = { bars: 9 }; const calls = []; let decorators = 0; let decoratorEnds = 0; let interactions = 0;",
                "function pointToScreen() { return null; }",
                "function beginIndicatorDrawingDecoratorFrame() { decorators += 1; return {}; }",
                "function endIndicatorDrawingDecoratorFrame() { decoratorEnds += 1; }",
                "function drawingGeometryRenderable(object) { return object?.geometryIntegrity?.status !== 'invalid'; }",
                "function drawDrawingObject(_ctx, object, _toScreen, _width, _pad, _preview, axis) { calls.push({ id: object.id, axis }); }",
                "function drawDrawingInteraction() { interactions += 1; }",
                "function intervalMinutesFromState() { return 5; }",
                "function rightGapBars() { return 7; }",
                shared_source,
                primary_source,
                "const visible = { bars: [{ ts: '2026-08-14T10:00:00.000Z' }], allBars: [{ ts: '2026-08-14T10:00:00.000Z' }], start: 0 };",
                "const primaryBefore = JSON.stringify(state.drawing);",
                "const lensAxis = drawDrawingObjects({}, [secondaryObject, syncingObject], visible, { left: 0, right: 0, top: 0 }, 100, 4, index => index * 4, price => price, 400, { snapshot: secondarySnapshot, slotStep: 1, rightGapBars: 0, timeframe: '1m', barsVersion: 3, selectedId: null, showHandles: false, decorators: false, renderableOnly: true });",
                "assert.deepEqual(calls.map(item => item.id), ['secondary', 'syncing'], 'Lens must retain presentation geometry while anchor verification is pending');",
                "assert.equal(lensAxis.snapshot, secondarySnapshot); assert.equal(lensAxis.rightGapBars, 0); assert.equal(lensAxis.slotStep, 1); assert.equal(lensAxis.timeframe, '1m'); assert.equal(lensAxis.barsVersion, 3);",
                "assert.equal(lensAxis.selectedId, null); assert.equal(lensAxis.showHandles, false); assert.equal(decorators, 0); assert.equal(decoratorEnds, 0);",
                "assert.equal(JSON.stringify(state.drawing), primaryBefore, 'isolated renderer must not mutate primary drawing state');",
                "calls.length = 0; drawDrawings({}, visible, { left: 0, right: 0, top: 0 }, 100, 4, index => index * 4, price => price, 400, { staticOnly: true });",
                "assert.deepEqual(calls.map(item => item.id), ['primary'], 'primary wrapper must still render primary objects');",
                "assert.equal(calls[0].axis.snapshot, primarySnapshot); assert.equal(calls[0].axis.rightGapBars, 7); assert.equal(calls[0].axis.slotStep, 5); assert.equal(calls[0].axis.selectedId, 'primary'); assert.equal(calls[0].axis.showHandles, true);",
                "assert.equal(decorators, 1); assert.equal(decoratorEnds, 1); assert.equal(interactions, 0, 'static primary pass must not draw interactions');",
                "drawDrawings({}, visible, { left: 0, right: 0, top: 0 }, 100, 4, index => index * 4, price => price, 400);",
                "assert.equal(interactions, 1, 'non-static primary wrapper must preserve its interaction pass');",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_drawing_load_is_atomic_scope_fenced_and_event_driven(
    tmp_path: Path,
) -> None:
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    drawing_runtime_source = (
        "function mtfLensDrawingScopeKey(scope = mtfLensRuntime.scope)"
        + lens_source.split("function mtfLensDrawingScopeKey(scope = mtfLensRuntime.scope)", 1)[
            1
        ].split("function mtfLensNormalizeTailMembership", 1)[0]
    )
    reset_source = (
        "function mtfLensResetConnectionOrder(runtime = mtfLensRuntime)"
        + lens_source.split("function mtfLensResetConnectionOrder(runtime = mtfLensRuntime)", 1)[
            1
        ].split("function mtfLensSocketIsCurrent", 1)[0]
    )
    handler_source = (
        "function mtfLensHandleStreamMessage(message, scope, epoch, connectionEpoch, socket)"
        + lens_source.split(
            "function mtfLensHandleStreamMessage(message, scope, epoch, connectionEpoch, socket)",
            1,
        )[1].split("function mtfLensRefreshRoute", 1)[0]
    )
    script_path = tmp_path / "mtf-lens-drawing-load.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const MTF_LENS_DRAWING_REFRESH_MAX_ATTEMPTS = 4;",
                "const MTF_LENS_DRAWING_REFRESH_BASE_DELAY_MS = 160;",
                "const localStorage = {}; const timers = new Map(); let timerSequence = 0;",
                "const window = { setTimeout(callback) { timerSequence += 1; timers.set(timerSequence, callback); return timerSequence; }, clearTimeout(timer) { timers.delete(timer); } };",
                "const primaryObject = { id: 'primary-object' };",
                "const state = { serverSleeping: false, drawing: { objects: [primaryObject] } };",
                "const scopeA = Object.freeze({ instrumentId: 'instrument-a', routeFingerprint: 'route-a', timeframe: '1m' });",
                "const scopeB = Object.freeze({ instrumentId: 'instrument-a', routeFingerprint: 'route-b', timeframe: '1m' });",
                "const oldDrawing = { id: 'old', instrument_id: 'instrument-a', route_fingerprint: 'route-a', timeframe: '1m' };",
                "const socket = {}; const mtfLensRuntime = { open: true, scope: scopeA, epoch: 1, connectionEpoch: 1, generation: 1, sequence: 5, canonicalRevision: 7, chartFrameReady: true, orderedBars: [{ ts: '2026-08-14T10:00:00.000Z' }], drawings: [oldDrawing], drawingsHydrated: false, drawingExactRefreshPending: false, drawingRequest: null, drawingRequestEpoch: 0, drawingReloadQueued: false, drawingRefreshTimer: null, drawingRefreshAttempts: 0, drawingRefreshScopeKey: '', socket };",
                "const requests = []; const renders = []; const resolverCalls = []; const queuedResponses = [];",
                "function exactIdentityText(value) { return typeof value === 'string' ? value : ''; }",
                "function mtfLensScopeEquals(left, right) { return Boolean(left && right) && left.instrumentId === right.instrumentId && left.routeFingerprint === right.routeFingerprint && left.timeframe === right.timeframe; }",
                "function normalizeDrawingObjects(rows) { if (!Array.isArray(rows)) throw new TypeError('malformed drawings'); return rows.map(item => { if (!item || item.malformed) throw new TypeError('malformed drawing'); return { ...item }; }); }",
                "function resolveDrawingAnchorsAgainstSnapshot(drawings, snapshot, options) { resolverCalls.push({ drawings, snapshot, options }); return true; }",
                "function mtfLensScheduleInteractionRender() { renders.push(mtfLensRuntime.drawings.map(item => item.id)); }",
                "function requestErrorMessage(error, fallback) { return error?.message || fallback; }",
                "function sharedObjectsBroadcastKey(instrumentId, timeframe, routeFingerprint) { return `objects:${instrumentId}:${timeframe}:${routeFingerprint}`; }",
                "function fetchServerStorage(instrumentId, timeframe, options) { const response = queuedResponses.shift(); if (!response) throw new Error('missing queued response'); const promise = typeof response === 'function' ? response() : response; requests.push({ instrumentId, timeframe, options, promise }); return promise; }",
                "function deferred() { let resolve; let reject; const promise = new Promise((resolvePromise, rejectPromise) => { resolve = resolvePromise; reject = rejectPromise; }); return { promise, resolve, reject }; }",
                "function payload(drawings, scope = scopeA) { return { ok: true, instrument_id: scope.instrumentId, route_fingerprint: scope.routeFingerprint, interval: scope.timeframe, drawings }; }",
                "function mtfLensSocketIsCurrent() { return true; }",
                "function mtfLensMessageAdmission(_message, _scope, order) { return { ok: true, drop: false, unsequenced: false, messageType: 'chart_bars', streamGeneration: 1, streamSeq: order.sequence + 1 }; }",
                "function mtfLensScheduleStaleCheck() {} function mtfLensApplyChartStatus() {} function mtfLensRequestFreshInitial() {} function mtfLensSetStatus() {} function mtfLensScheduleRender() {}",
                "function mtfLensCommitBarFrame(runtime) { runtime.chartFrameReady = true; runtime.canonicalRevision = 8; return { ok: true }; }",
                drawing_runtime_source,
                reset_source,
                handler_source,
                "(async () => {",
                "  const primaryBefore = JSON.stringify(state.drawing);",
                "  queuedResponses.push(Promise.resolve(payload([])));",
                "  assert.equal(await mtfLensLoadDrawings(scopeA, 1), true);",
                "  assert.deepEqual(mtfLensRuntime.drawings, [], 'valid empty exact response must atomically clear the Lens collection');",
                "  assert.equal(requests[0].instrumentId, 'instrument-a'); assert.equal(requests[0].timeframe, '1m');",
                "  assert.equal(requests[0].options.settings, false); assert.equal(requests[0].options.alerts, false); assert.equal(requests[0].options.isolated, true); assert.equal(requests[0].options.expectedRouteFingerprint, 'route-a');",
                "  assert.equal(requests[0].options.signal instanceof AbortSignal, true);",
                "  assert.equal(JSON.stringify(state.drawing), primaryBefore, 'exact Lens storage must not mutate primary drawing state');",
                "  mtfLensRuntime.drawings = [oldDrawing]; queuedResponses.push(Promise.resolve(payload({ malformed: true })));",
                "  assert.equal(await mtfLensLoadDrawings(scopeA, 1, { force: true }), false);",
                "  assert.deepEqual(mtfLensRuntime.drawings, [oldDrawing], 'malformed response must preserve the last admitted collection');",
                "  queuedResponses.push(Promise.reject(new Error('isolated storage unavailable')));",
                "  assert.equal(await mtfLensLoadDrawings(scopeA, 1, { force: true }), false);",
                "  assert.deepEqual(mtfLensRuntime.drawings, [oldDrawing], 'rejected response must preserve the last admitted collection');",
                "  queuedResponses.push(Promise.resolve(payload([], scopeB)));",
                "  assert.equal(await mtfLensLoadDrawings(scopeA, 1, { force: true }), false);",
                "  assert.deepEqual(mtfLensRuntime.drawings, [oldDrawing], 'wrong exact scope must preserve the last admitted collection');",
                "  const lateClose = deferred(); queuedResponses.push(lateClose.promise);",
                "  const closeLoad = mtfLensLoadDrawings(scopeA, 1, { force: true }); const closeSignal = requests.at(-1).options.signal;",
                "  mtfLensCancelDrawingRuntime({ clear: true }); mtfLensRuntime.open = false; mtfLensRuntime.scope = null; mtfLensRuntime.epoch = 2;",
                "  assert.equal(closeSignal.aborted, true, 'close must abort the exact in-flight storage request'); assert.equal(mtfLensRuntime.drawingRequest, null);",
                "  lateClose.resolve(payload([{ ...oldDrawing, id: 'late-close' }])); assert.equal(await closeLoad, false);",
                "  assert.deepEqual(mtfLensRuntime.drawings, [], 'late close response must remain fenced');",
                "  mtfLensRuntime.open = true; mtfLensRuntime.scope = scopeA; mtfLensRuntime.epoch = 3; mtfLensRuntime.drawings = [oldDrawing];",
                "  const lateRebind = deferred(); queuedResponses.push(lateRebind.promise);",
                "  const rebindLoad = mtfLensLoadDrawings(scopeA, 3, { force: true }); const rebindSignal = requests.at(-1).options.signal;",
                "  mtfLensCancelDrawingRuntime({ clear: true }); mtfLensRuntime.scope = scopeB; mtfLensRuntime.epoch = 4;",
                "  assert.equal(rebindSignal.aborted, true, 'rebind must abort the previous exact-scope request');",
                "  lateRebind.resolve(payload([{ ...oldDrawing, id: 'late-rebind' }])); assert.equal(await rebindLoad, false);",
                "  assert.deepEqual(mtfLensRuntime.drawings, [], 'late rebind response must remain fenced');",
                "  mtfLensRuntime.scope = scopeA; mtfLensRuntime.epoch = 5; mtfLensRuntime.drawings = [oldDrawing];",
                "  const requestCount = requests.length;",
                "  assert.equal(mtfLensHandleSharedObjectsStorageEvent({ key: 'objects:other', storageArea: localStorage }), false); assert.equal(requests.length, requestCount);",
                "  queuedResponses.push(Promise.resolve(payload([{ ...oldDrawing, id: 'broadcast' }])));",
                "  assert.equal(mtfLensHandleSharedObjectsStorageEvent({ key: sharedObjectsBroadcastKey('instrument-a', '1m', 'route-a'), storageArea: localStorage }), true);",
                "  await requests.at(-1).promise; await new Promise(resolve => setImmediate(resolve));",
                "  assert.deepEqual(mtfLensRuntime.drawings.map(item => item.id), ['broadcast'], 'matching exact storage event must reload the Lens collection');",
                "  assert.equal(mtfLensRuntime.drawingsHydrated, true); assert.equal(mtfLensRuntime.drawingExactRefreshPending, false); assert.equal(mtfLensRuntime.drawingRefreshTimer, null);",
                "  queuedResponses.push(Promise.reject(new Error('transient broadcast failure')));",
                "  assert.equal(mtfLensHandleSharedObjectsStorageEvent({ key: sharedObjectsBroadcastKey('instrument-a', '1m', 'route-a'), storageArea: localStorage }), true);",
                "  await new Promise(resolve => setImmediate(resolve));",
                "  const retryTimer = mtfLensRuntime.drawingRefreshTimer; assert.ok(retryTimer && timers.has(retryTimer), 'failed forced storage-event reload must schedule a bounded retry');",
                "  assert.equal(mtfLensRuntime.drawingsHydrated, true, 'transient reload failure must preserve the previously hydrated collection');",
                "  assert.equal(mtfLensRuntime.drawingExactRefreshPending, true, 'failed exact event demand must remain explicit');",
                "  const reconnectRecovery = deferred(); queuedResponses.push(reconnectRecovery.promise); const requestCountBeforeReconnect = requests.length;",
                "  const reconnectEpoch = mtfLensResetConnectionOrder(mtfLensRuntime);",
                "  assert.equal(reconnectEpoch, 2); assert.equal(mtfLensRuntime.chartFrameReady, false); assert.equal(mtfLensRuntime.canonicalRevision, 0); assert.equal(mtfLensRuntime.drawingExactRefreshPending, true, 'candle reconnect reset must preserve exact storage-event demand');",
                "  mtfLensHandleStreamMessage({ type: 'chart_bars' }, scopeA, 5, reconnectEpoch, socket);",
                "  assert.equal(requests.length, requestCountBeforeReconnect + 1, 'first accepted reconnect frame must force the retained exact demand');",
                "  assert.equal(requests.at(-1).options.force, true); assert.equal(requests.at(-1).options.reason, 'mtf_lens_first_chart_frame');",
                "  assert.equal(mtfLensRuntime.drawingRefreshTimer, retryTimer, 'ordinary first-frame resolution must not complete storage retry ownership'); assert.equal(timers.has(retryTimer), true); assert.equal(mtfLensRuntime.drawingExactRefreshPending, true, 'intent must remain until exact response admission');",
                "  reconnectRecovery.resolve(payload([{ ...oldDrawing, id: 'broadcast-recovered' }])); await requests.at(-1).promise; await new Promise(resolve => setImmediate(resolve));",
                "  assert.deepEqual(mtfLensRuntime.drawings.map(item => item.id), ['broadcast-recovered']);",
                "  assert.equal(mtfLensRuntime.drawingExactRefreshPending, false, 'valid exact payload must retire the explicit event demand'); assert.equal(mtfLensRuntime.drawingRefreshTimer, null, 'valid exact payload alone must complete the owned retry'); assert.equal(timers.has(retryTimer), false); assert.equal(mtfLensRuntime.drawingRefreshAttempts, 0); assert.equal(mtfLensRuntime.drawingRefreshScopeKey, '');",
                "  assert.equal(JSON.stringify(state.drawing), primaryBefore, 'all Lens object lifecycles must leave primary state untouched');",
                "  assert.ok(resolverCalls.length >= 2); assert.ok(renders.length >= 2);",
                "})().catch(error => { console.error(error); process.exitCode = 1; });",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_first_chart_frame_retries_unhydrated_or_pending_drawing_scope(
    tmp_path: Path,
) -> None:
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    handler_source = (
        "function mtfLensHandleStreamMessage(message, scope, epoch, connectionEpoch, socket)"
        + lens_source.split(
            "function mtfLensHandleStreamMessage(message, scope, epoch, connectionEpoch, socket)",
            1,
        )[1].split("function mtfLensRefreshRoute", 1)[0]
    )
    reset_source = (
        "function mtfLensResetConnectionOrder(runtime = mtfLensRuntime)"
        + lens_source.split("function mtfLensResetConnectionOrder(runtime = mtfLensRuntime)", 1)[
            1
        ].split("function mtfLensSocketIsCurrent", 1)[0]
    )
    script_path = tmp_path / "mtf-lens-first-frame-drawing-retry.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const scope = { instrumentId: 'instrument-a', routeFingerprint: 'route-a', timeframe: '1m' }; const socket = {};",
                "const mtfLensRuntime = { socket, connectionEpoch: 3, chartFrameReady: false, drawingsHydrated: false, drawingExactRefreshPending: false, drawingRequest: null, drawingRefreshTimer: null, drawingRefreshAttempts: 0, drawingRefreshScopeKey: '', drawings: [{ id: 'old' }], generation: 2, sequence: 4, canonicalRevision: 6, orderedBars: [] };",
                "const loads = []; const admittedOrders = []; let resolves = 0; let renders = 0;",
                "function mtfLensSocketIsCurrent() { return true; }",
                "function mtfLensMessageAdmission(_message, _scope, order) { admittedOrders.push({ ...order }); return { ok: true, drop: false, unsequenced: false, messageType: 'chart_bars', streamGeneration: 2, streamSeq: 5 }; }",
                "function mtfLensScheduleStaleCheck() {} function mtfLensApplyChartStatus() {} function mtfLensRequestFreshInitial() {} function mtfLensSetStatus() {}",
                "function mtfLensCommitBarFrame(runtime) { runtime.chartFrameReady = true; runtime.canonicalRevision = 7; return { ok: true }; }",
                "function mtfLensResolveDrawings() { resolves += 1; }",
                "function mtfLensLoadDrawings(loadedScope, epoch, options) { loads.push({ loadedScope, epoch, options }); return Promise.resolve(true); }",
                "function mtfLensScheduleRender() { renders += 1; }",
                reset_source,
                handler_source,
                "const frame = { type: 'chart_bars', canonical_revision: 7 };",
                "mtfLensHandleStreamMessage(frame, scope, 9, 3, socket);",
                "assert.equal(admittedOrders[0].canonicalRevision, 6, 'admission must be fenced by the currently committed canonical revision');",
                "assert.equal(loads.length, 1, 'first accepted frame must retry a failed pre-frame drawing load'); assert.equal(loads[0].loadedScope, scope); assert.equal(loads[0].epoch, 9); assert.equal(loads[0].options.force, true); assert.equal(loads[0].options.reason, 'mtf_lens_first_chart_frame');",
                "assert.equal(resolves, 1); assert.equal(renders, 1);",
                "mtfLensRuntime.chartFrameReady = false; mtfLensRuntime.drawingsHydrated = true; mtfLensHandleStreamMessage(frame, scope, 9, 3, socket);",
                "assert.equal(loads.length, 1, 'an already hydrated exact collection must not be fetched again');",
                "mtfLensRuntime.chartFrameReady = false; mtfLensRuntime.drawingsHydrated = false; mtfLensRuntime.drawingRequest = { promise: Promise.resolve(false) }; mtfLensHandleStreamMessage(frame, scope, 9, 3, socket);",
                "assert.equal(loads.length, 1, 'an in-flight exact load must not be duplicated by the first frame');",
                "mtfLensRuntime.drawingRequest = null; mtfLensRuntime.chartFrameReady = true; mtfLensRuntime.drawingsHydrated = true; mtfLensRuntime.drawingExactRefreshPending = true; mtfLensRuntime.drawingRefreshTimer = 77; mtfLensRuntime.drawingRefreshAttempts = 1; mtfLensRuntime.drawingRefreshScopeKey = 'pending-exact'; mtfLensRuntime.generation = 8; mtfLensRuntime.sequence = 12; mtfLensRuntime.canonicalRevision = 9;",
                "const reconnectLoadCount = loads.length; const reconnectEpoch = mtfLensResetConnectionOrder(mtfLensRuntime);",
                "assert.equal(reconnectEpoch, 4); assert.equal(mtfLensRuntime.canonicalRevision, 0); assert.equal(mtfLensRuntime.chartFrameReady, false); assert.equal(mtfLensRuntime.drawingExactRefreshPending, true, 'candle reconnect reset must preserve exact storage-event demand');",
                "mtfLensHandleStreamMessage(frame, scope, 9, reconnectEpoch, socket);",
                "assert.equal(loads.length, reconnectLoadCount + 1, 'first accepted reconnect frame must force the retained exact demand');",
                "const reconnectLoad = loads.at(-1); assert.equal(reconnectLoad.loadedScope, scope); assert.equal(reconnectLoad.epoch, 9); assert.equal(reconnectLoad.options.force, true); assert.equal(reconnectLoad.options.reason, 'mtf_lens_first_chart_frame');",
                "assert.equal(mtfLensRuntime.drawingExactRefreshPending, true, 'pending intent must remain until a valid exact payload is admitted');",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_scope_admission_and_checkpoint_merge_are_deterministic(
    tmp_path: Path,
) -> None:
    candle_source = Path("src/aef_terminal/ui/assets/js/20-live-candle-sync.js").read_text(
        encoding="utf-8"
    )
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    normalize_source = (
        "function normalizeChartStreamBarForScope(bar, expectedScope, streamOrder = null)"
        + candle_source.split(
            "function normalizeChartStreamBarForScope(bar, expectedScope, streamOrder = null)",
            1,
        )[1].split("function streamBarKind", 1)[0]
    )
    membership_source = (
        "function mtfLensNormalizeTailMembership(value)"
        + lens_source.split("function mtfLensNormalizeTailMembership(value)", 1)[1].split(
            "function mtfLensMessageAdmission", 1
        )[0]
    )
    admission_source = (
        "function mtfLensMessageAdmission(message, expectedScope, currentOrder = {})"
        + lens_source.split(
            "function mtfLensMessageAdmission(message, expectedScope, currentOrder = {})",
            1,
        )[1].split("function mtfLensTimestampKey", 1)[0]
    )
    commit_source = (
        "function mtfLensTimestampKey(value)"
        + lens_source.split("function mtfLensTimestampKey(value)", 1)[1].split(
            "function mtfLensNodes", 1
        )[0]
    )
    terminal_status_source = (
        "function mtfLensStatusIsTerminal(message)"
        + lens_source.split("function mtfLensStatusIsTerminal(message)", 1)[1].split(
            "function mtfLensApplyChartStatus", 1
        )[0]
    )
    socket_order_source = (
        "function mtfLensResetConnectionOrder(runtime = mtfLensRuntime)"
        + lens_source.split("function mtfLensResetConnectionOrder(runtime = mtfLensRuntime)", 1)[
            1
        ].split("function mtfLensStatusIsTerminal", 1)[0]
    )
    script_path = tmp_path / "mtf-lens-admission.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                'const MTF_LENS_CONSUMER_ROLE = "secondary_candles";',
                "const MTF_LENS_TAIL_BARS = 300;",
                "const MTF_LENS_MAX_BARS = 500;",
                "const state = { instrumentId: 'instrument-a', timeframe: '5m' };",
                "function exactIdentityText(value) { return typeof value === 'string' ? value : ''; }",
                "function instrumentRouteFingerprint() { return 'route-a'; }",
                "function mtfLensScopeEquals(left, right) { return left === right; }",
                'function barIsGapPlaceholder(bar) { return Boolean(bar?.missing || bar?.preview_kind === "gap_placeholder"); }',
                "function resolveStreamBar(_existing, incoming) { return incoming; }",
                normalize_source,
                membership_source,
                admission_source,
                commit_source,
                terminal_status_source,
                socket_order_source,
                "const scope = Object.freeze({ instrumentId: 'instrument-a', routeFingerprint: 'route-a', timeframe: '1m', consumerRole: MTF_LENS_CONSUMER_ROLE, range: '1d', tailBars: MTF_LENS_TAIL_BARS });",
                "const bar = (ts, timeframe = '1m', route = 'route-a') => ({ instrument_id: 'instrument-a', route_fingerprint: route, provider_symbol: 'ES', timeframe, ts, open: 100, high: 102, low: 99, close: 101, volume: 0, source: 'ibkr:provider', closed: true, state: 'confirmed', authoritative: true, commit_pending: false });",
                "const provisional = ts => ({ ...bar(ts), closed: false, state: 'forming', authoritative: false });",
                "const mainBar = bar('2026-08-14T10:00:00.000Z', '5m');",
                "assert.equal(normalizeStreamBar(mainBar)?.timeframe, '5m', 'primary wrapper must preserve main scope');",
                "assert.equal(normalizeStreamBar(bar('2026-08-14T10:01:00.000Z')), null, 'primary wrapper must reject Lens timeframe');",
                "assert.equal(normalizeChartStreamBarForScope(bar('2026-08-14T10:01:00.000Z'), scope)?.timeframe, '1m');",
                "assert.equal(normalizeChartStreamBarForScope(bar('2026-08-14T10:01:00.000Z', '1m', 'route-b'), scope), null);",
                "const frame = (bars, sequence, recoveryScope = '', generation = 1, timestamps = undefined, limit = MTF_LENS_TAIL_BARS, canonicalRevision = undefined) => { const payload = { type: 'chart_bars', instrument_id: scope.instrumentId, route_fingerprint: scope.routeFingerprint, interval: scope.timeframe, range: scope.range, consumer_role: scope.consumerRole, bars, recovery: Boolean(recoveryScope), recovery_complete: Boolean(recoveryScope), recovery_scope: recoveryScope, canonical_revision: canonicalRevision ?? Math.max(Number(runtime.canonicalRevision || 0), 1), stream_generation: generation, stream_seq: sequence }; if (recoveryScope) payload.secondary_tail_membership = { limit, timestamps: timestamps ?? bars.map(item => item.ts) }; return payload; };",
                "const runtime = { epoch: 7, generation: 0, sequence: 0, canonicalRevision: 0, barsByTs: new Map(), orderedBars: [] };",
                "const t1 = '2026-08-14T10:01:00.000Z'; const t2 = '2026-08-14T10:02:00.000Z'; const t3 = '2026-08-14T10:03:00.000Z'; const t4 = '2026-08-14T10:04:00.000Z'; const t5 = '2026-08-14T10:05:00.000Z';",
                "const liveBeforeInitial = frame([provisional(t4)], 1);",
                "const liveAdmission = mtfLensMessageAdmission(liveBeforeInitial, scope, runtime);",
                "assert.equal(liveAdmission.ok, true); assert.equal(mtfLensCommitBarFrame(runtime, liveBeforeInitial, liveAdmission).ok, true);",
                "assert.equal(runtime.canonicalRevision, 1, 'accepted bar frame must establish the isolated drawing generation');",
                "const first = frame([bar(t1), bar(t2), bar(t3)], 2, 'initial', 1, [t1, t2, t3]);",
                "const firstAdmission = mtfLensMessageAdmission(first, scope, runtime);",
                "assert.equal(firstAdmission.ok, true);",
                "assert.equal(mtfLensCommitBarFrame(runtime, first, firstAdmission).ok, true);",
                "assert.deepEqual(runtime.orderedBars.map(item => item.ts), [t1, t2, t3, t4], 'initial recovery must preserve a newer typed live projection');",
                "const checkpoint = frame([], 3, 'checkpoint', 1, [t1, t2, t3]);",
                "const checkpointAdmission = mtfLensMessageAdmission(checkpoint, scope, runtime);",
                "assert.equal(mtfLensCommitBarFrame(runtime, checkpoint, checkpointAdmission).ok, true);",
                "assert.deepEqual(runtime.orderedBars.map(item => item.ts), [t1, t2, t3, t4], 'empty recurring checkpoint must retain authoritative members and newer projection');",
                "assert.equal(mtfLensMessageAdmission(checkpoint, scope, runtime).drop, true, 'replayed sequence must be dropped');",
                "const aged = frame([], 4, 'checkpoint', 1, [t2, t3]);",
                "assert.equal(mtfLensCommitBarFrame(runtime, aged, mtfLensMessageAdmission(aged, scope, runtime)).ok, true);",
                "assert.deepEqual(runtime.orderedBars.map(item => item.ts), [t2, t3, t4], 'aged-out confirmed members must be removed');",
                "const deleted = frame([], 5, 'checkpoint', 1, [t3]);",
                "assert.equal(mtfLensCommitBarFrame(runtime, deleted, mtfLensMessageAdmission(deleted, scope, runtime)).ok, true);",
                "assert.deepEqual(runtime.orderedBars.map(item => item.ts), [t3, t4], 'deleted confirmed members must be removed');",
                "const emptyTail = frame([], 6, 'checkpoint', 1, []);",
                "assert.equal(mtfLensCommitBarFrame(runtime, emptyTail, mtfLensMessageAdmission(emptyTail, scope, runtime)).ok, true);",
                "assert.deepEqual(runtime.orderedBars.map(item => item.ts), [t4], 'empty authoritative membership must retain only typed provisional state');",
                "const replacement = frame([bar(t5)], 1, 'initial', 2, [t5], MTF_LENS_TAIL_BARS, 4);",
                "const replacementAdmission = mtfLensMessageAdmission(replacement, scope, runtime);",
                "assert.equal(mtfLensCommitBarFrame(runtime, replacement, replacementAdmission).ok, true);",
                "assert.deepEqual(runtime.orderedBars.map(item => item.ts), [t5], 'initial recovery must discard confirmed and provisional state outside its authoritative boundary');",
                "assert.equal(runtime.canonicalRevision, 4, 'canonical revision must advance with the committed frame');",
                "const regressedCanonical = frame([], 2, 'checkpoint', 2, [t5], MTF_LENS_TAIL_BARS, 3);",
                "assert.equal(mtfLensMessageAdmission(regressedCanonical, scope, runtime).ok, false, 'same-connection canonical revision regression must fail before mutation');",
                "assert.equal(runtime.canonicalRevision, 4, 'rejected canonical regression must preserve the accepted revision');",
                "assert.equal(mtfLensMessageAdmission({ ...replacement, route_fingerprint: 'route-b', stream_seq: 2 }, scope, runtime).ok, false);",
                "assert.equal(mtfLensMessageAdmission({ ...replacement, consumer_role: 'primary', stream_seq: 2 }, scope, runtime).ok, false);",
                "assert.equal(mtfLensMessageAdmission({ ...replacement, range: '5d', stream_seq: 2 }, scope, runtime).ok, false, 'wrong range must be rejected');",
                "assert.equal(mtfLensMessageAdmission({ ...replacement, bars: [bar(t3, '5m')], stream_seq: 2 }, scope, runtime).ok, false);",
                "assert.equal(mtfLensMessageAdmission(frame([], 2, 'checkpoint', 2, [], 301), scope, runtime).reasonCode, 'tail_membership_profile_mismatch');",
                "const noMembership = frame([], 2, 'checkpoint', 2, []); delete noMembership.secondary_tail_membership; assert.equal(mtfLensMessageAdmission(noMembership, scope, runtime).reasonCode, 'tail_membership_missing');",
                "assert.equal(mtfLensMessageAdmission({ ...frame([], 2), secondary_tail_membership: { limit: 300, timestamps: [] } }, scope, runtime).reasonCode, 'tail_membership_not_recovery');",
                "assert.equal(mtfLensMessageAdmission({ ...frame([], 2, 'checkpoint', 2, []), secondary_tail_membership: { limit: 300, timestamps: [], extra: true } }, scope, runtime).reasonCode, 'tail_membership_shape_invalid');",
                "assert.equal(mtfLensMessageAdmission(frame([], 2, 'checkpoint', 2, [], 63), scope, runtime).reasonCode, 'tail_membership_limit_invalid');",
                "assert.equal(mtfLensMessageAdmission(frame([], 2, 'checkpoint', 2, [t2, t1]), scope, runtime).reasonCode, 'tail_membership_order_invalid');",
                "assert.equal(mtfLensMessageAdmission(frame([], 2, 'checkpoint', 2, ['2026-08-14T10:00:00+01:00']), scope, runtime).reasonCode, 'tail_membership_timestamp_invalid');",
                "assert.equal(mtfLensMessageAdmission(frame([bar(t2)], 2, 'checkpoint', 2, [t1]), scope, runtime).reasonCode, 'tail_membership_recovery_bar_invalid', 'recovery bar outside membership must fail closed');",
                "assert.equal(mtfLensMessageAdmission(frame([provisional(t2)], 2, 'checkpoint', 2, [t2]), scope, runtime).reasonCode, 'tail_membership_recovery_bar_invalid', 'recovery bars must be authoritative confirmed');",
                "assert.equal(mtfLensMessageAdmission(frame([{ ...bar(t2), authoritative: false }], 2, 'checkpoint', 2, [t2]), scope, runtime).reasonCode, 'tail_membership_recovery_bar_invalid');",
                "const missingMember = frame([], 2, 'checkpoint', 2, ['2026-08-14T10:06:00.000Z']); const beforeMissing = runtime.orderedBars.map(item => item.ts); const missingCommit = mtfLensCommitBarFrame(runtime, missingMember, mtfLensMessageAdmission(missingMember, scope, runtime)); assert.equal(missingCommit.reconnect, true); assert.equal(missingCommit.reasonCode, 'tail_membership_member_missing'); assert.deepEqual(runtime.orderedBars.map(item => item.ts), beforeMissing, 'failed reconciliation must not mutate runtime');",
                "const confirmedTimes = Array.from({ length: 300 }, (_, index) => new Date(Date.UTC(2026, 7, 15) + index * 60000).toISOString());",
                "const provisionalTimes = Array.from({ length: 250 }, (_, index) => new Date(Date.UTC(2026, 7, 15) + (300 + index) * 60000).toISOString());",
                "const cappedRuntime = { epoch: 1, generation: 0, sequence: 0, barsByTs: new Map(provisionalTimes.map(ts => [ts, provisional(ts)])), orderedBars: [] };",
                "const cappedInitial = frame(confirmedTimes.map(ts => bar(ts)), 1, 'initial', 1, confirmedTimes); const cappedCommit = mtfLensCommitBarFrame(cappedRuntime, cappedInitial, mtfLensMessageAdmission(cappedInitial, scope, cappedRuntime)); assert.equal(cappedCommit.ok, true); assert.equal(cappedRuntime.orderedBars.length, MTF_LENS_MAX_BARS, 'Lens projection must never exceed the browser cap'); assert.equal(confirmedTimes.every(ts => cappedRuntime.barsByTs.has(ts)), true, 'authoritative membership must survive projection bounding'); assert.equal(cappedRuntime.orderedBars.filter(item => !mtfLensBarIsAuthoritativeConfirmed(item)).length, 200);",
                "const status = { type: 'chart_status', instrument_id: scope.instrumentId, route_fingerprint: scope.routeFingerprint, interval: scope.timeframe, range: scope.range, consumer_role: scope.consumerRole, source: 'ibkr:status', message: 'waiting', retry_in_seconds: 1, stream_generation: 0, stream_seq: 0 }; assert.equal(mtfLensMessageAdmission(status, scope, runtime).ok, true); assert.equal(mtfLensMessageAdmission({ ...status, range: '5d' }, scope, runtime).ok, false);",
                "assert.equal(mtfLensStatusIsTerminal({ type: 'chart_status', retry_in_seconds: 0, requires_resubscribe: false }), true, 'terminal status must block reconnect');",
                "assert.equal(mtfLensStatusIsTerminal({ type: 'chart_status', retry_in_seconds: 1 }), false);",
                "assert.equal(mtfLensStatusIsTerminal({ type: 'chart_status', retry_in_seconds: 0, requires_resubscribe: true }), false);",
                "runtime.open = true; runtime.scope = scope; runtime.epoch = 7; runtime.connectionEpoch = 9; runtime.generation = 9; runtime.sequence = 99; runtime.canonicalRevision = 14;",
                "const oldSocket = {}; const newSocket = {}; runtime.socket = oldSocket;",
                "const connectionEpoch = mtfLensResetConnectionOrder(runtime);",
                "assert.equal(connectionEpoch, 10); assert.equal(runtime.generation, 0); assert.equal(runtime.sequence, 0); assert.equal(runtime.canonicalRevision, 0, 'connection restart must reset isolated drawing generation');",
                "runtime.socket = newSocket;",
                "assert.equal(mtfLensSocketIsCurrent(runtime, oldSocket, scope, 7, 9), false, 'old socket callback must be fenced');",
                "assert.equal(mtfLensSocketIsCurrent(runtime, newSocket, scope, 7, connectionEpoch), true);",
                "const afterRestart = frame([bar(t5)], 1, 'initial', 1, [t5]);",
                "const restartAdmission = mtfLensMessageAdmission(afterRestart, scope, { epoch: connectionEpoch, generation: runtime.generation, sequence: runtime.sequence });",
                "assert.equal(restartAdmission.ok, true); assert.equal(restartAdmission.drop, false, 'new socket lower server generation must be accepted');",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_presentation_reconciles_open_runtime_and_bounds_canvas_memory(
    tmp_path: Path,
) -> None:
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    presentation_source = (
        "function mtfLensApplyStoredPresentation()"
        + lens_source.split("function mtfLensApplyStoredPresentation()", 1)[1].split(
            "function mtfLensPersistFrameSize", 1
        )[0]
    )
    pixel_ratio_source = (
        "function mtfLensCanvasPixelRatio(width, height)"
        + lens_source.split("function mtfLensCanvasPixelRatio(width, height)", 1)[1].split(
            "function mtfLensFormatPrice", 1
        )[0]
    )
    script_path = tmp_path / "mtf-lens-presentation.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const MTF_LENS_CANVAS_PIXEL_BUDGET = 10000000;",
                "const window = { devicePixelRatio: 2 };",
                "const mtfLensRuntime = { open: false };",
                "const calls = [];",
                "function mtfLensAnchorValue() { calls.push('anchor-value'); return 'top-right'; }",
                "function mtfLensApplyAnchor(anchor, persist) { calls.push(['anchor', anchor, persist]); }",
                "function mtfLensRestoreFrameSize() { calls.push('size'); }",
                "function mtfLensScheduleRender() { calls.push('render'); }",
                presentation_source,
                pixel_ratio_source,
                "assert.equal(mtfLensApplyStoredPresentation(), false); assert.deepEqual(calls, [], 'closed Lens must ignore presentation reconciliation');",
                "mtfLensRuntime.open = true; assert.equal(mtfLensApplyStoredPresentation(), true);",
                "assert.deepEqual(calls, ['anchor-value', ['anchor', 'top-right', false], 'size', 'render'], 'authoritative settings must update geometry and recolor the base canvas without persisting');",
                "assert.equal(mtfLensCanvasPixelRatio(640, 360), 2, 'normal Lens keeps DPR2');",
                "const boundedRatio = mtfLensCanvasPixelRatio(2400, 1800);",
                "assert.ok(boundedRatio >= 1 && boundedRatio < 2);",
                "assert.ok((2 * 2400 * 1800 * boundedRatio * boundedRatio) <= MTF_LENS_CANVAS_PIXEL_BUDGET + 1, 'two backing stores stay inside the aggregate pixel budget');",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_catalog_rollover_fences_before_route_mutation_and_rebinds_once(
    tmp_path: Path,
) -> None:
    reference_source = Path("src/aef_terminal/ui/assets/js/19-reference-data.js").read_text(
        encoding="utf-8"
    )
    load_source = (
        "async function loadInstruments(options = {})"
        + reference_source.split("async function loadInstruments(options = {})", 1)[1].split(
            "async function addWatchlistInstrumentFromForm", 1
        )[0]
    )
    script_path = tmp_path / "mtf-lens-route-rollover.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const events = [];",
                "let catalog = [{ instrument_id: 'instrument-a', route_fingerprint: 'route-a', provider: 'ibkr', key: 'ES' }];",
                "let nextPayload = { watchlist_version: 2, items: catalog.map(item => ({ ...item })) };",
                "const state = { instrumentId: 'instrument-a', watchlistVersion: 1, timeframe: '5m', range: '1d', dataSource: 'ibkr', symbol: 'ES', paperTrading: { armed: false }, instrumentSelectionStatus: { state: 'resolved' } };",
                "Object.defineProperty(state, 'instruments', { get() { return catalog; }, set(value) { catalog = value; events.push(`catalog:${value[0]?.route_fingerprint || ''}`); } });",
                "const view = { barsVisible: 100 };",
                "const serverStorageReady = false;",
                "function exactIdentityText(value) { return typeof value === 'string' ? value.trim() : ''; }",
                "function instrumentForId(instrumentId = state.instrumentId) { return state.instruments.find(item => item.instrument_id === instrumentId) || null; }",
                "function instrumentRouteFingerprint() { return exactIdentityText(instrumentForId()?.route_fingerprint); }",
                "function reconcileWatchlistPresentations(items) { return items.map(item => ({ ...item })); }",
                "async function fetchJson() { events.push('fetch'); return nextPayload; }",
                "function debugStep(_name, detail = '') { events.push(detail ? `debug:${detail}` : 'debug'); }",
                "function apiErrorMessage(_payload, fallback) { return fallback; }",
                "function requestErrorMessage(_error, fallback) { return fallback; }",
                "function mtfLensPrepareForPrimaryScopeChange() { events.push('prepare'); }",
                "function mtfLensRebindCurrentScope() { events.push(`rebind:${instrumentRouteFingerprint()}:${state.dataSource}`); }",
                "function closeQuoteStream() { events.push('close-quote'); }",
                "function pruneWatchlistTrendState() { events.push('prune'); }",
                "function clearUiNotice() { events.push('clear-notice'); }",
                "function workspaceSet() { events.push('workspace'); }",
                "function currentOptionDriveState() { return { armed: false }; }",
                "function currentOptionBoardState() { return { surface: '' }; }",
                "async function stopGexLiveContext() { events.push('stop-gex'); }",
                "function storedRangeForInstrument() { return '5d'; }",
                "function storedBarsVisibleForInstrument() { return 120; }",
                "function syncWorkspacePageState() { events.push('sync-workspace'); }",
                "function clearChartStateForRouteSwitch() { events.push('clear-chart'); }",
                "function reconcileOptionDriveInstrumentCatalog() { events.push('reconcile-options'); }",
                "function renderInstruments() { events.push('render-instruments'); }",
                "function renderGexSchedulerInstrumentOptions() { events.push('render-gex-options'); }",
                load_source,
                "(async () => {",
                "  await loadInstruments({ activate: false });",
                "  assert.equal(events.includes('prepare'), false, 'unchanged exact route must not fence Lens');",
                "  assert.equal(events.some(item => item.startsWith('rebind:')), false, 'unchanged exact route must not reconnect Lens');",
                "  events.length = 0;",
                "  nextPayload = { watchlist_version: 3, items: [{ instrument_id: 'instrument-a', route_fingerprint: 'route-b', provider: 'ibkr', key: 'ESM6' }] };",
                "  await loadInstruments({ activate: false });",
                "  assert.equal(events.filter(item => item === 'prepare').length, 1);",
                "  assert.equal(events.filter(item => item.startsWith('rebind:')).length, 1);",
                "  assert.ok(events.indexOf('prepare') < events.indexOf('catalog:route-b'), 'old socket must be fenced before catalog route mutation');",
                "  assert.ok(events.indexOf('catalog:route-b') < events.indexOf('rebind:route-b:ibkr'), 'Lens must rebind only after the selected route and provider settle');",
                "  assert.ok(events.indexOf('rebind:route-b:ibkr') < events.indexOf('stop-gex'), 'Lens route fence must not wait behind later async teardown');",
                "})().catch(error => { console.error(error); process.exitCode = 1; });",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_mtf_lens_opened_during_server_sleep_connects_on_wake(tmp_path: Path) -> None:
    lens_source = Path("src/aef_terminal/ui/assets/js/23-mtf-lens.js").read_text(encoding="utf-8")
    connect_source = (
        "function mtfLensConnect(scope, epoch)"
        + lens_source.split("function mtfLensConnect(scope, epoch)", 1)[1].split(
            "function mtfLensBindScope", 1
        )[0]
    )
    wake_source = (
        "function mtfLensHandleServerSleepState()"
        + lens_source.split("function mtfLensHandleServerSleepState()", 1)[1].split(
            "function mtfLensSyncTimeframeButtons", 1
        )[0]
    )
    script_path = tmp_path / "mtf-lens-sleep-wake.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "const MTF_LENS_TAIL_BARS = 300;",
                "const scope = { instrumentId: 'instrument-a', routeFingerprint: 'route-a', timeframe: '1m', consumerRole: 'secondary_candles', range: '1d', tailBars: MTF_LENS_TAIL_BARS };",
                "const state = { serverSleeping: true };",
                "const mtfLensRuntime = { open: true, scope, epoch: 4, connectionEpoch: 0, socket: null, reconnectTimer: null, reconnectBlocked: false, reconnectBlockedForSleep: false, blockedSleepObserved: false, reconnectAttempt: 0, drawings: [{ id: 'old' }] };",
                "const statuses = []; const drawingLoads = []; let socketCount = 0; let drawingCancels = 0;",
                "const window = { location: { protocol: 'http:', host: 'terminal.test' } };",
                "class WebSocket { constructor(url) { this.url = url; this.readyState = 0; socketCount += 1; } close() { this.readyState = 3; } }",
                "function mtfLensScopeEquals(left, right) { return left === right; }",
                "function mtfLensSetStatus(message) { statuses.push(message); }",
                "function mtfLensDisconnectTransport() { mtfLensRuntime.socket = null; mtfLensRuntime.connectionEpoch += 1; }",
                "function mtfLensScheduleReconnect() {}",
                "function mtfLensSocketIsCurrent() { return true; }",
                "function mtfLensScheduleStaleCheck() {}",
                "function mtfLensHandleStreamMessage() {}",
                "function mtfLensCancelDrawingRuntime() { drawingCancels += 1; mtfLensRuntime.drawings = []; }",
                "function mtfLensScheduleInteractionRender() {}",
                "function mtfLensLoadDrawings(loadedScope, epoch, options) { drawingLoads.push({ loadedScope, epoch, options }); return Promise.resolve(true); }",
                connect_source,
                wake_source,
                "mtfLensConnect(scope, 4);",
                "assert.equal(socketCount, 0); assert.equal(mtfLensRuntime.reconnectBlocked, true); assert.equal(mtfLensRuntime.reconnectBlockedForSleep, true); assert.equal(mtfLensRuntime.blockedSleepObserved, true);",
                "mtfLensHandleServerSleepState(); assert.equal(drawingCancels, 1, 'sleep must abort and clear the isolated drawing runtime'); assert.deepEqual(mtfLensRuntime.drawings, []);",
                "state.serverSleeping = false; mtfLensHandleServerSleepState();",
                "assert.equal(socketCount, 1, 'wake must connect a Lens opened while sleep was already active');",
                "assert.equal(mtfLensRuntime.reconnectBlocked, false); assert.equal(mtfLensRuntime.reconnectBlockedForSleep, false);",
                "assert.equal(drawingLoads.length, 1, 'wake must reload the exact secondary drawing scope'); assert.equal(drawingLoads[0].loadedScope, scope); assert.equal(drawingLoads[0].epoch, 4); assert.equal(drawingLoads[0].options.force, true); assert.equal(drawingLoads[0].options.reason, 'mtf_lens_wake');",
                "assert.ok(mtfLensRuntime.socket.url.includes('range=1d')); assert.ok(mtfLensRuntime.socket.url.includes('tail_bars=300'));",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_three_minute_timeframe_is_exposed_as_virtual_chart_choice() -> None:
    constants = Path("src/aef_terminal/ui/assets/js/04-constants.js").read_text(encoding="utf-8")

    assert '{ label: "3M", interval: "3m" }' in constants
    assert '"3m": ["5d", "7d", "14d", "31d", "2mo", "3mo"]' in constants
    assert '"3m": "5d"' in constants
