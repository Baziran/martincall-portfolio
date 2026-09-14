import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / "src/aef_terminal/indicators/modules/market_spotlight/client.js"
STYLES_PATH = ROOT / "src/aef_terminal/indicators/modules/market_spotlight/styles.css"


def _run_client_script(tmp_path: Path, body: str) -> dict:
    client = CLIENT_PATH.read_text(encoding="utf-8")
    script = tmp_path / "market_spotlight_brief_test.js"
    script.write_text(
        "\n".join(
            [
                "const state = { snapshot: null, indicators: {}, instrumentId: 'instrument-es', timeframe: '5m', symbol: 'ES' };",
                "function exactIdentityText(value) { return String(value || '').trim(); }",
                "function instrumentRouteFingerprint() { return 'route-es'; }",
                "function tradeSetupRuntimeModel(snapshot) { return snapshot.trade_setup_model || null; }",
                "function activeGexContext(snapshot) { return snapshot.gex || null; }",
                "function indicatorCalcForId() { return true; }",
                "function indicatorVisibleForId() { return true; }",
                "function registerIndicatorTableModel() {}",
                "const registeredIndicatorTableHeaderIcons = new Map();",
                "const registeredIndicatorTableActions = new Map();",
                "function registerIndicatorTableHeaderIcon(ref, renderer, options = {}) { registeredIndicatorTableHeaderIcons.set(ref, { renderer, options }); }",
                "function registerIndicatorTableAction(action, handlers) { registeredIndicatorTableActions.set(action, handlers); }",
                "function registerIndicatorTableAnimation() {}",
                "function registerIndicatorControlEffect() {}",
                "function registerIndicatorOverlayContribution() {}",
                "function registerIndicatorPanelRenderer() {}",
                "function uiMotionEnabled() { return false; }",
                "function renderPriceOverlay() {}",
                "function bumpChartRenderVersion() {}",
                "function escapeHtml(value) { return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); }",
                client,
                body,
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_market_spotlight_brief_reuses_typed_owners_and_projects_transient_lines(
    tmp_path: Path,
) -> None:
    result = _run_client_script(
        tmp_path,
        """
const snapshot = {
  bars: [
    { ts: '2026-08-20T13:30:00Z', close: 100 },
    { ts: '2026-08-20T13:35:00Z', close: 102 },
  ],
  indicators: {
    market_spotlight: {
      latest: { ts: '2026-08-20T13:35:00Z' },
      market_context: {
        direction: 'long',
        state: 'directional_flow',
        closed: true,
        flow_state: { directive: 'follow_flow' },
        risk: { state: 'clear', tone: 'clear' },
        entry: {
          trigger: 101,
          relation: 'above',
          directive: 'wait_closed_bar_confirmation',
          direction: 'long',
        },
        invalidation: {
          level: 96,
          relation: 'below',
          direction: 'long',
        },
        vwap: { base: 99, upper_2: 105, lower_2: 93 },
        opening_range: { ready: true, high: 103, low: 97 },
      },
    },
  },
  trade_setup_model: {
    authorityReady: true,
    blocked: false,
    displayPhase: 'ARM',
    side: 'long',
    entry: 101,
    stop: 96,
    target: 107,
  },
  gex: {
    decision_authoritative: false,
    gamma_flip: 100,
    levels: [
      { kind: 'CALL_WALL', strike: 110 },
      { kind: 'PUT_WALL', strike: 90 },
    ],
  },
};
state.snapshot = snapshot;
marketSpotlightBriefState.showScenario = true;
const levels = marketSpotlightBriefLevels(snapshot);
const overlays = marketSpotlightScenarioOverlays(snapshot);
console.log(JSON.stringify({
  levels: levels.map(level => ({
    key: level.key,
    price: level.price,
    aliases: level.aliases,
    source: level.source,
    sources: level.sources,
    overlay: level.overlay,
  })),
  overlays,
}));
""",
    )

    levels = result["levels"]
    overlays = result["overlays"]
    by_price = {level["price"]: level for level in levels}

    assert by_price[101]["aliases"] == ["TS ENTRY", "MS TRIGGER"]
    assert by_price[101]["sources"] == ["trade_setup", "market_spotlight"]
    assert by_price[96]["aliases"] == ["TS STOP", "MS INV"]
    assert by_price[96]["sources"] == ["trade_setup", "market_spotlight"]
    assert {level["key"] for level in levels} >= {
        "trade_entry",
        "trade_stop",
        "trade_target",
        "gex_call_wall",
        "gex_put_wall",
        "gex_zero_gamma",
        "vwap",
        "opening_range_high",
        "opening_range_low",
    }
    assert all(item["type"] == "line" for item in overlays)
    assert all(item["source"] == "market_spotlight" for item in overlays)
    assert all(item["role"] == "scenario_reference" for item in overlays)
    assert all(item["advisory_only"] is True for item in overlays)
    assert all(item["signal_overlay"] is False for item in overlays)
    assert all(item["start_ts"] == "2026-08-20T13:30:00Z" for item in overlays)
    assert all(item["end_anchor_ts"] == "2026-08-20T13:35:00Z" for item in overlays)
    assert {item["price"] for item in overlays} == {90, 96, 97, 99, 100, 101, 103, 107, 110}


def test_market_spotlight_beacon_owns_the_brief_action(tmp_path: Path) -> None:
    result = _run_client_script(
        tmp_path,
        """
const icon = registeredIndicatorTableHeaderIcons.get('beacon');
const action = registeredIndicatorTableActions.get('market-spotlight-open-brief');
console.log(JSON.stringify({
  iconAction: icon?.options?.action || '',
  hasClick: typeof action?.click === 'function',
}));
""",
    )

    assert result == {
        "iconAction": "market-spotlight-open-brief",
        "hasClick": True,
    }


def test_market_spotlight_brief_is_package_owned_and_non_durable() -> None:
    client = CLIENT_PATH.read_text(encoding="utf-8")
    styles = STYLES_PATH.read_text(encoding="utf-8")

    assert 'registerIndicatorOverlayContribution("market_spotlight_brief"' in client
    assert 'registerIndicatorTableAction("market-spotlight-open-brief"' in client
    assert 'action: "market-spotlight-open-brief"' in client
    assert (
        'registerIndicatorPanelRenderer(\n  "market_spotlight",\n  renderMarketSpotlightPanel'
        in client
    )
    assert "marketSpotlightBriefLevels(snapshot)" in client
    assert "tradeSetupRuntimeModel(snapshot)" in client
    assert "activeGexContext(snapshot)" in client
    assert 'data-market-spotlight-brief-action="scenario"' in client
    assert "касание уровня не равно подтверждению" in client
    assert "advisory_only: true" in client
    assert "signal_overlay: false" in client
    assert "localStorage" not in client
    assert "setServerSettingValue" not in client
    assert "saveDrawing" not in client
    assert "market-spotlight-brief-launch-button" not in client
    assert ".market-spotlight-brief-launch-button" not in styles
    assert ".market-spotlight-brief-table" in styles
    assert ".market-spotlight-brief-scenario.active" in styles
    assert ".market-spotlight-brief-layer[hidden]" in styles
