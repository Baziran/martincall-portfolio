from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from aef_terminal.indicators.module_discovery import (
    discover_indicator_modules,
    indicator_module_asset_paths,
)
from aef_terminal.ui import asset_services


TICK_FLOW_PACKAGE = Path("src/aef_terminal/indicators/modules/tick_flow")


def _package_source(name: str) -> str:
    return (TICK_FLOW_PACKAGE / name).read_text(encoding="utf-8")


def test_tick_flow_discovers_its_complete_frontend_asset_bundle() -> None:
    module = next(module for module in discover_indicator_modules() if module.id == "tick_flow")
    discovered_paths = tuple(
        path for path in indicator_module_asset_paths("js") if path.parent.name == "tick_flow"
    )

    assert module.ui_js_assets == ("runtime.js", "overlays.js")
    assert module.spec.settings_scope == "instrument"
    assert all(control.scope == "instrument" for control in module.spec.controls)
    assert tuple(path.name for path in discovered_paths) == module.ui_js_assets

    assembled_source = asset_services.martincall_js_source_sync()
    for path in discovered_paths:
        assert path.read_text(encoding="utf-8").rstrip() in assembled_source
    assert assembled_source.index("const TICK_FLOW_INDICATOR_ID") < assembled_source.index(
        'registerIndicatorCanvasHook(\n  "tick_flow",\n  "price_profile"'
    )


def test_tick_flow_uses_canonical_timeframe_step_without_shared_dead_helpers() -> None:
    core_source = Path("src/aef_terminal/ui/assets/js/40-chart-core.js").read_text(encoding="utf-8")
    overlays_source = _package_source("overlays.js")
    runtime_source = _package_source("runtime.js")

    assert "barStepMs" not in core_source
    assert "rightEdgeBarSlot" not in core_source
    assert "barStepMs" not in overlays_source
    assert "barStepMs" not in runtime_source
    assert "const step = Math.max(intervalMsFromState(), 60_000);" in overlays_source
    assert "const stepMs = Math.max(intervalMsFromState(), 60_000);" in runtime_source


def test_tick_flow_runtime_owns_effect_and_lifecycle_registration() -> None:
    source = _package_source("runtime.js")

    assert 'registerIndicatorProcessEffect("tick_flow_context", ({ group }) => {' in source
    assert 'registerIndicatorControlEffect(\n      "tick_flow_live",' in source
    for effect_ref in (
        "tick_flow_recent_delta",
        "tick_flow_recent_delta_seconds",
        "tick_flow_context_reload",
    ):
        assert f'"{effect_ref}"' in source
    assert "registerIndicatorLifecycleSync(" in source
    assert "syncTickFlowLifecycle,\n      { poll: true }," in source
    assert "registerIndicatorSettingsSync(" in source
    assert "registerIndicatorPanelRenderer(" in source
    assert source.count("TICK_FLOW_INDICATOR_ID,") >= 3


def test_generic_lifecycle_polling_is_opt_in_and_failure_isolated() -> None:
    host = Path("src/aef_terminal/ui/assets/js/30-panels-indicator-controls.js").read_text(
        encoding="utf-8"
    )
    ai_client = Path("src/aef_terminal/indicators/modules/ai_third_opinion/client.js").read_text(
        encoding="utf-8"
    )

    assert ".filter(([, entry]) => !options.poll || entry.poll)" in host
    assert "await Promise.allSettled(" in host
    assert "indicator lifecycle failed:" in host
    assert (
        'registerIndicatorLifecycleSync("ai_third_opinion", '
        "syncAiThirdOpinionModuleLifecycle);" in ai_client
    )


def test_tick_flow_calc_off_stops_live_collection_and_clears_context() -> None:
    source = _package_source("runtime.js")
    lifecycle = source.split(
        "async function syncTickFlowLifecycle(options = {})",
        1,
    )[1].split("registerIndicatorLifecycleSync(", 1)[0]
    disabled_branch = lifecycle.split("if (!group.enabled) {", 1)[1].split(
        "return;",
        1,
    )[0]

    assert "group.live = false;" in disabled_branch
    assert 'saveInstrumentIndicatorSetting("tickFlowLive", "false");' in disabled_branch
    assert "clearTickContext();" in disabled_branch
    assert "await syncTickLiveMode(false);" in disabled_branch

    process_effect = source.split(
        'registerIndicatorProcessEffect("tick_flow_context"',
        1,
    )[1].split("registerIndicatorControlEffect(", 1)[0]
    assert "if (!group?.enabled && group?.live)" in process_effect
    assert "group.live = false;" in process_effect
    assert 'saveInstrumentIndicatorSetting("tickFlowLive", "false");' in process_effect
    assert "void syncTickFlowLifecycle({" in process_effect


def test_tick_flow_polling_loads_context_only_while_active() -> None:
    source = _package_source("runtime.js")
    lifecycle = source.split(
        "async function syncTickFlowLifecycle(options = {})",
        1,
    )[1].split("registerIndicatorLifecycleSync(", 1)[0]
    load_context = source.split(
        "async function loadTickContextOnce(snapshot = state.snapshot, options = {})",
        1,
    )[1].split(
        "async function loadTickContext(snapshot = state.snapshot, options = {})",
        1,
    )[0]

    assert lifecycle.index("if (!group.enabled) {") < lifecycle.index(
        "await loadTickContext(snapshot"
    )
    assert "&& (!options.poll || group.live)" in lifecycle
    assert "if (!group?.enabled || !snapshot?.bars?.length)" in load_context
    assert "if (!tickContextPollingActive(options))" in load_context
    assert "return backgroundPollingActive();" in source
    assert "state.tickContextDesiredRequest = {" in source
    assert "if (state.tickContextRequestPromise)" in source
    assert "while (state.tickContextDesiredRequest)" in source
    assert "result = await loadTickContextOnce(" in source


def test_tick_live_latches_terminal_errors_and_bounds_retryable_failures(
    tmp_path: Path,
) -> None:
    runtime = _package_source("runtime.js")
    shared_labels = Path("src/aef_terminal/ui/assets/js/06-labels.js").read_text(encoding="utf-8")
    script = "\n".join(
        (
            shared_labels,
            """
const posts = [];
const saved = [];
const deferredResponses = [];
const lifecycleHandlers = new Map();
let runtimeStateResolver = null;
const state = {
  symbol: "SPY",
  instrumentId: "ibkr|contract|756733",
  routeFingerprint: "route-a",
  snapshot: { bars: [] },
  indicators: {
    tickFlow: {
      enabled: true,
      live: true,
      lookbackMinutes: 120,
    },
  },
};
const window = {};
const document = {
  getElementById() { return null; },
  querySelector() { return null; },
  createElement() { return {}; },
};
let responseMode = "terminal";
let nowMs = 100000;
Date.now = () => nowMs;

function ensureManagedIndicatorStateGroups() {}
function exactIdentityText(value) {
  return typeof value === "string" ? value : "";
}
function instrumentRouteFingerprint() {
  return state.routeFingerprint;
}
function apiErrorMessage(payload, fallback) {
  return payload?.error?.message || fallback;
}
function requestErrorMessage(error, fallback) {
  return error?.payload?.error?.message || error?.message || fallback;
}
function setUiNotice() {}
function clearUiNotice() {}
function saveInstrumentIndicatorSetting(key, value) {
  saved.push([key, value]);
}
function renderCharts() {}
function applySettings() {}
function registerIndicatorRuntimeStateRef(id, resolver) {
  if (id === "tick_flow_service") runtimeStateResolver = resolver;
}
function registerIndicatorLifecycleSync(id, handler) {
  lifecycleHandlers.set(id, handler);
}
function registerIndicatorSettingsSync() {}
function registerIndicatorPanelRenderer() {}
function registerIndicatorProcessEffect() {}
function registerIndicatorControlEffect() {}

async function fetchJson(url, options) {
  if (url !== "/api/ticks/live" || options?.method !== "POST") {
    throw new Error(`unexpected request: ${url}`);
  }
  const request = JSON.parse(options.body);
  posts.push(request);
  const base = {
    instrument_id: request.instrument_id,
    route_fingerprint: request.expected_route_fingerprint,
  };
  if (responseMode === "deferred") {
    return new Promise(resolve => {
      deferredResponses.push({ request, resolve });
    });
  }
  if (!request.enabled) {
    return {
      ...base,
      ok: true,
      live: { enabled: false, running: false, status: "off" },
    };
  }
  if (responseMode === "starting") {
    return {
      ...base,
      ok: true,
      live: { enabled: true, running: false, status: "starting" },
    };
  }
  if (responseMode === "scope_mismatch") {
    const mismatchedRoute = `${request.expected_route_fingerprint}-other`;
    return {
      ...base,
      route_fingerprint: mismatchedRoute,
      ok: true,
      live: {
        enabled: true,
        running: true,
        status: "live",
        route_identities: [{
          instrument_id: request.instrument_id,
          route_fingerprint: mismatchedRoute,
        }],
      },
    };
  }
  const retryable = responseMode === "retryable";
  return {
    ...base,
    ok: false,
    error: {
      code: retryable ? "SERVER_SLEEPING" : "TICK_LIVE_PAYLOAD_INVALID",
      category: "tick",
      retryable,
      message: retryable ? "sleeping" : "invalid",
    },
    live: {
      enabled: false,
      running: false,
      status: retryable ? "sleeping" : "error",
      last_error: retryable ? "sleeping" : "invalid",
    },
  };
}
""",
            runtime,
            """
(async () => {
  const lifecycle = lifecycleHandlers.get("tick_flow");
  if (typeof lifecycle !== "function") throw new Error("lifecycle missing");

  await lifecycle({ poll: true });
  const terminalPosts = posts.length;
  const terminalKey = state.tickLiveTerminalErrorKey;
  await lifecycle({ poll: true });
  await lifecycle({ poll: true });
  const terminalHeartbeatPosts = posts.length - terminalPosts;

  responseMode = "starting";
  state.routeFingerprint = "route-b";
  await lifecycle({ poll: true });
  const routeChangePosts = posts.length - terminalPosts;
  const routeChangeClearedLatch = state.tickLiveTerminalErrorKey === "";
  const startingPosts = posts.length;
  await lifecycle({ poll: true });
  await lifecycle({ poll: true });
  const startingHeartbeatPosts = posts.length - startingPosts;

  responseMode = "terminal";
  await syncTickLiveMode(true, { manual: true });
  const manualTerminalKey = state.tickLiveTerminalErrorKey;
  const beforeManualRetry = posts.length;
  responseMode = "starting";
  await syncTickLiveMode(true, { manual: true });
  const manualRetryPosts = posts.length - beforeManualRetry;
  const manualClearedLatch = state.tickLiveTerminalErrorKey === "";

  responseMode = "deferred";
  state.routeFingerprint = "route-deferred-a";
  const beforeDeferred = posts.length;
  const deferredRouteAPromise = syncTickLiveMode(true, { manual: true });
  for (let index = 0; index < 10 && deferredResponses.length < 1; index += 1) {
    await Promise.resolve();
  }
  if (deferredResponses.length !== 1) {
    throw new Error("route A request was not deferred");
  }
  const routeARequestKey = JSON.stringify([
    state.instrumentId,
    "route-deferred-a",
    "live",
  ]);
  state.routeFingerprint = "route-deferred-b";
  const deferredRouteBPromise = syncTickLiveMode(true);
  const routeAResponse = deferredResponses[0];
  routeAResponse.resolve({
    instrument_id: routeAResponse.request.instrument_id,
    route_fingerprint: routeAResponse.request.expected_route_fingerprint,
    ok: false,
    error: {
      code: "TICK_LIVE_PAYLOAD_INVALID",
      category: "tick",
      retryable: false,
      message: "late route A failure",
    },
    live: {
      enabled: false,
      running: false,
      status: "error",
      last_error: "late route A failure",
    },
  });
  for (let index = 0; index < 10 && deferredResponses.length < 2; index += 1) {
    await Promise.resolve();
  }
  if (deferredResponses.length !== 2) {
    throw new Error("route B request was not queued after route A");
  }
  const statusAfterLateRouteA = state.tickLiveStatus;
  const terminalAfterLateRouteA = state.tickLiveTerminalErrorKey;
  const settledAfterLateRouteA = state.tickLiveSettledKey;
  const routeBResponse = deferredResponses[1];
  routeBResponse.resolve({
    instrument_id: routeBResponse.request.instrument_id,
    route_fingerprint: routeBResponse.request.expected_route_fingerprint,
    ok: true,
    live: {
      enabled: true,
      running: true,
      status: "live",
      route_identities: [{
        instrument_id: routeBResponse.request.instrument_id,
        route_fingerprint: routeBResponse.request.expected_route_fingerprint,
      }],
    },
  });
  await Promise.all([deferredRouteAPromise, deferredRouteBPromise]);
  const deferredPosts = posts.slice(beforeDeferred);
  const statusAfterRouteB = state.tickLiveStatus;
  const routeBTerminalCleared = state.tickLiveTerminalErrorKey === "";

  responseMode = "scope_mismatch";
  state.routeFingerprint = "route-scope";
  const beforeScopeMismatch = posts.length;
  await lifecycle({ poll: true });
  const scopeMismatchPosts = posts.length - beforeScopeMismatch;
  const scopeMismatchStatus = state.tickLiveStatus;
  const scopeMismatchTerminal = state.tickLiveTerminalErrorKey === JSON.stringify([
    state.instrumentId,
    "route-scope",
    "live",
  ]);
  await lifecycle({ poll: true });
  await lifecycle({ poll: true });
  const scopeMismatchHeartbeatPosts = posts.length - beforeScopeMismatch - scopeMismatchPosts;

  state.routeFingerprint = "route-render-current";
  state.tickLiveStatus = {
    enabled: true,
    running: true,
    status: "live",
    route_identities: [{
      instrument_id: state.instrumentId,
      route_fingerprint: "route-render-foreign",
    }],
  };
  const foreignRouteStatus = tickFlowStatusFact();
  const foreignRoutePresentation = tickFlowStatusPresentation(foreignRouteStatus);
  const foreignRouteRuntime = runtimeStateResolver();

  responseMode = "retryable";
  state.routeFingerprint = "route-c";
  await lifecycle({ poll: true });
  const retryStartPosts = posts.length;
  for (let index = 0; index < 4; index += 1) {
    nowMs += 1000;
    await lifecycle({ poll: true });
  }
  const oneSecondRetryPosts = posts.length - retryStartPosts;
  nowMs = 105000;
  await lifecycle({ poll: true });
  nowMs = 120000;
  await lifecycle({ poll: true });
  nowMs = 150000;
  await lifecycle({ poll: true });
  const boundedRetryPosts = posts.length - retryStartPosts;
  nowMs = 999999;
  await lifecycle({ poll: true });
  const postsAfterExhaustion = posts.length - retryStartPosts;
  const retryExhausted = state.tickLiveRetryState?.exhausted === true;

  const beforeCalcOff = posts.length;
  state.indicators.tickFlow.enabled = false;
  state.indicators.tickFlow.live = true;
  await lifecycle({ poll: true });
  await lifecycle({ poll: true });
  await lifecycle({ poll: true });
  const calcOffPosts = posts.slice(beforeCalcOff);
  const persistedLiveFalse = saved.filter(
    ([key, value]) => key === "tickFlowLive" && value === "false"
  ).length;

  console.log(JSON.stringify({
    terminalPosts,
    terminalHeartbeatPosts,
    terminalLatched: Boolean(terminalKey),
    routeChangePosts,
    routeChangeClearedLatch,
    startingHeartbeatPosts,
    manualLatched: Boolean(manualTerminalKey),
    manualRetryPosts,
    manualClearedLatch,
    deferredRoutes: deferredPosts.map(
      request => request.expected_route_fingerprint
    ),
    lateRouteAStatus: statusAfterLateRouteA?.status,
    lateRouteAStatusRoute: statusAfterLateRouteA?.route_identities?.[0]?.route_fingerprint,
    lateRouteATerminalLatched: terminalAfterLateRouteA === routeARequestKey,
    lateRouteASettled: settledAfterLateRouteA === routeARequestKey,
    routeBStatus: statusAfterRouteB?.status,
    routeBStatusRoute: statusAfterRouteB?.route_identities?.[0]?.route_fingerprint,
    routeBTerminalCleared,
    scopeMismatchPosts,
    scopeMismatchHeartbeatPosts,
    scopeMismatchTerminal,
    scopeMismatchStatus: scopeMismatchStatus?.status,
    scopeMismatchReason: scopeMismatchStatus?.reason_code,
    foreignRouteCode: foreignRouteStatus.code,
    foreignRouteTone: foreignRoutePresentation.tone,
    foreignRouteRuntimeText: foreignRouteRuntime.text,
    foreignRouteRuntimeClass: foreignRouteRuntime.actionClass,
    oneSecondRetryPosts,
    boundedRetryPosts,
    postsAfterExhaustion,
    retryExhausted,
    calcOffPostCount: calcOffPosts.length,
    calcOffEnabled: calcOffPosts[0]?.enabled,
    persistedLiveFalse,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
""",
        )
    )
    script_path = tmp_path / "tick-flow-live-settlement.js"
    script_path.write_text(script, encoding="utf-8")

    completed = subprocess.run(
        ["node", str(script_path)],
        cwd=".",
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "terminalPosts": 1,
        "terminalHeartbeatPosts": 0,
        "terminalLatched": True,
        "routeChangePosts": 1,
        "routeChangeClearedLatch": True,
        "startingHeartbeatPosts": 0,
        "manualLatched": True,
        "manualRetryPosts": 1,
        "manualClearedLatch": True,
        "deferredRoutes": ["route-deferred-a", "route-deferred-b"],
        "lateRouteAStatus": "starting",
        "lateRouteAStatusRoute": "route-deferred-b",
        "lateRouteATerminalLatched": False,
        "lateRouteASettled": False,
        "routeBStatus": "live",
        "routeBStatusRoute": "route-deferred-b",
        "routeBTerminalCleared": True,
        "scopeMismatchPosts": 1,
        "scopeMismatchHeartbeatPosts": 0,
        "scopeMismatchTerminal": True,
        "scopeMismatchStatus": "error",
        "scopeMismatchReason": "tick_live_response_scope_mismatch",
        "foreignRouteCode": "error",
        "foreignRouteTone": "bad",
        "foreignRouteRuntimeText": "ERROR",
        "foreignRouteRuntimeClass": "bad",
        "oneSecondRetryPosts": 0,
        "boundedRetryPosts": 3,
        "postsAfterExhaustion": 3,
        "retryExhausted": True,
        "calcOffPostCount": 1,
        "calcOffEnabled": False,
        "persistedLiveFalse": 1,
    }


def test_tick_live_retry_contract_uses_typed_fields_only() -> None:
    source = _package_source("runtime.js")

    assert "state.tickLiveSettledKey === requestKey && !retryDue" in source
    assert "state.tickLiveTerminalErrorKey === requestKey" in source
    assert "typedError?.retryable === true" in source
    assert "typedError ? typedError.retryable === true : true" in source
    assert "await syncTickLiveMode(requested, { manual: true });" in source
    assert "state.tickLiveScopeKey !== scopeKey" in source
    assert "responseScopeMatches" in source
    assert "state.tickLiveKey" not in source
    assert '"error", "starting"' not in source
    assert ".includes(String(state.tickLiveStatus" not in source


def test_tick_flow_status_uses_typed_fact_and_shared_tooltip_lines() -> None:
    source = _package_source("runtime.js")

    assert "function tickFlowStatusFact" in source
    assert "function tickFlowStatusPresentation" in source
    assert 'code: "live"' in source
    assert 'reason_code: "tick_collector_live"' in source
    assert "uiPresentationStateDescriptor(status?.code)" in source
    assert "lines: presentation.lines" in source
    assert "error: presentation.error" in source
    assert "tickFlowStatusText" not in source
    assert "status.text" not in source
    assert "status.tooltip" not in source
    assert 'split("\\n")' not in source
    assert "TICK_FLOW_STATUS_PRESENTATION" not in source


def test_tick_flow_status_codes_resolve_through_shared_presentation_owner(
    tmp_path: Path,
) -> None:
    runtime_source = _package_source("runtime.js")
    status_fact_source = runtime_source.split("function tickFlowStatusFact", 1)[1].split(
        "function tickFlowStatusPresentation", 1
    )[0]
    emitted_codes = sorted(set(re.findall(r'\bcode:\s*["\']([^"\']+)["\']', status_fact_source)))
    assert emitted_codes == [
        "connecting",
        "degraded",
        "disabled",
        "error",
        "live",
        "loading",
        "pending",
        "ready",
        "sleeping",
        "unavailable",
    ]

    shared_labels = Path("src/aef_terminal/ui/assets/js/06-labels.js").read_text(encoding="utf-8")
    script = "\n".join(
        (
            shared_labels,
            f"const emittedCodes = {json.dumps(emitted_codes)};",
            "const resolved = emittedCodes.map(code => uiPresentationStateDescriptor(code));",
            "if (resolved.some(item => item.state === 'unknown')) process.exit(2);",
            "console.log(JSON.stringify(resolved));",
        )
    )
    script_path = tmp_path / "tick-flow-status-codes.js"
    script_path.write_text(script, encoding="utf-8")

    completed = subprocess.run(
        ["node", str(script_path)],
        cwd=".",
        text=True,
        capture_output=True,
        check=True,
    )
    resolved = json.loads(completed.stdout)

    assert [item["state"] for item in resolved] == emitted_codes
    assert all(item["label"] != "UNKNOWN" for item in resolved)
    assert all(item["tone"] in {"ok", "warn", "bad", "muted"} for item in resolved)


def test_tick_flow_package_registers_every_canvas_phase() -> None:
    source = _package_source("overlays.js")
    host = Path("src/aef_terminal/ui/assets/js/30-panels-indicator-controls.js").read_text(
        encoding="utf-8"
    )
    registrations = set(
        re.findall(
            r'registerIndicatorCanvasHook\(\s*"tick_flow",\s*"([^"]+)"',
            source,
        )
    )

    assert registrations == {
        "price_profile",
        "volume_overlay",
        "current_price_adornment",
    }
    assert "drawTickVolumeProfile(ctx, snapshot, pad, priceH, width, y);" in source
    assert "drawTickDelta(ctx, bars, pad, chartH, xStep);" in source
    assert "drawRecentTickDeltaOnPriceLine(" in source
    assert "(entry.requireCalc && !calcEnabled)" in host
    assert "(entry.requireVisible && !visible)" in host
    assert "Indicator canvas state key failed:" in host
    assert 'return [...prefix, "state_error"].join(":");' in host


def test_tick_flow_has_no_static_core_frontend_ownership() -> None:
    core_asset_names = tuple(path.name for path in asset_services.JS_ASSET_FILES)
    html = asset_services.HTML_TEMPLATE_FILE.read_text(encoding="utf-8")

    assert "16-tick-gex-runtime.js" not in core_asset_names
    assert "51-overlays-tick-flow.js" not in core_asset_names
    assert not any("tick-flow" in name or "tick_flow" in name for name in core_asset_names)
    for forbidden in (
        "Tick Flow",
        "tick-flow",
        "tick_flow",
        "tickFlow",
    ):
        assert forbidden not in html
