import re

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from aef_terminal.ui.asset_services import (
    HTML_TEMPLATE_FILE,
    martincall_css_source_sync,
    martincall_js_source_sync,
)


def workspace_dock_fixture_html() -> str:
    """Real browser assets with boot/network replaced by a presentation-only fixture."""
    js = martincall_js_source_sync().replace("    boot();", "")
    js += """
      const fixtureWrites = [];
      let fixtureRejectWrite = false;
      function setServerSettingValue(key, value) {
        if (fixtureRejectWrite) return false;
        if (!isBrowserWritableClientSettingKey(key)) throw new Error(key);
        fixtureWrites.push([key, value]);
        serverSettings[key] = value;
        return true;
      }
      setupOptionDrive();
      setupIndicatorLens();
      setupWorkspaceDockControls();
      setupGexDock();
      applyLayout();
      document.getElementById('settings-toggle').addEventListener('click', () => {
        document.getElementById('settings-panel').classList.toggle('open');
      });
      document.getElementById('settings-close').addEventListener('click', () => {
        document.getElementById('settings-panel').classList.remove('open');
      });
      updateIndicatorLensTables([{reason: 'dock', overlay: {
        source: 'fixture', table: {title: 'Layout fixture', columns: [
          {cells: ['STATE', 'Sample', 'Display only']},
          {cells: ['RANGE', 'Compact', 'No market data']},
        ]},
      }}]);
      window.dockFixture = {
        writes: fixtureWrites,
        rejectWrite(value) { fixtureRejectWrite = value; },
        state, layout,
      };
    """
    html = HTML_TEMPLATE_FILE.read_text(encoding="utf-8").replace("__DEBUG_CLASS__", "")
    html = html.replace("</head>", f"<style>{martincall_css_source_sync()}</style></head>")
    return html.replace("</body>", f"<script>{js}</script></body>")


@pytest.fixture
def dock_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "**/*",
            lambda route: route.fulfill(
                body=workspace_dock_fixture_html(), content_type="text/html"
            ),
        )
        page.goto("http://martincall.test/")
        yield page
        browser.close()
        assert not errors


def _box(page: Page, selector: str) -> dict:
    box = page.locator(selector).bounding_box()
    assert box is not None
    return box


def _load_lens_tooltips(page: Page, reason: str = "dock") -> None:
    page.evaluate(
        """reason => {
          registerIndicatorTableAction('fixture-tooltip-action', {
            click: payload => { window.fixtureClickedSurface = payload.surface; },
          });
          window.fixtureTooltipEntries = [{reason, overlay: {
            source: 'fixture', table: {
              title: 'Tooltip fixture', tooltip_mode: 'details',
              tooltip_sections: [{title: 'TABLE DETAILS', lines: [
                {text: 'CALL table context', role: 'positive', token_roles: {CALL: 'call'}},
              ]}],
              tooltip_tables: [{title: 'Levels', columns: ['Level', 'Value'],
                rows: [['Reference', '123.45']]}],
              columns: [
                {cells: ['STATE', 'Sample', 'Display only'], action: 'fixture-tooltip-action',
                  tooltip_sections: [{title: 'ROW DETAILS', lines: [
                    {text: 'PUT row context', role: 'negative', token_roles: {PUT: 'put'}},
                  ]}]},
                {cells: ['RANGE', 'Compact', 'No market data']},
              ],
            },
          }}];
          updateIndicatorLensTables(window.fixtureTooltipEntries);
        }""",
        reason,
    )


def test_mobile_toolbar_scroll_reaches_settings_control(dock_page: Page) -> None:
    page = dock_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("document.body.classList.add('view-preset-mobile')")

    toolbar = page.locator(".chart-tools")
    # Exercise overflow independently of platform font metrics at the same viewport.
    toolbar.evaluate("element => { element.style.width = '160px'; }")
    before = toolbar.evaluate(
        "element => ({ clientWidth: element.clientWidth, scrollWidth: element.scrollWidth })"
    )

    assert before["scrollWidth"] > before["clientWidth"]
    toolbar.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
    toolbar_box = _box(page, ".chart-tools")
    settings_box = _box(page, "#settings-toggle")

    assert settings_box["x"] >= toolbar_box["x"]
    assert settings_box["x"] + settings_box["width"] <= toolbar_box["x"] + toolbar_box["width"]
    assert toolbar.evaluate("element => getComputedStyle(element).touchAction") == "pan-x"


def test_touch_hold_opens_chart_context_request_and_drag_cancels_it(dock_page: Page) -> None:
    page = dock_page
    page.evaluate(
        """() => {
          const canvas = document.getElementById('price-chart');
          const bar = {
            ts: '2026-09-03T08:00:00.000Z',
            open: 100, high: 102, low: 99, close: 101, volume: 10,
            closed: true, state: 'confirmed', authoritative: true,
          };
          canvas.setPointerCapture = () => {};
          canvas.hasPointerCapture = () => false;
          canvas.releasePointerCapture = () => {};
          window.touchContextRequests = [];
          openChartContextMenu = (event, target) => {
            window.touchContextRequests.push({
              id: target.id,
              x: event.clientX,
              y: event.clientY,
              pointerType: event.pointerType,
            });
          };
          visibleBars = () => ({ bars: [bar] });
          hitTestManagedObjectLayers = () => null;
          canvasActionHit = () => null;
          paperTradingHandleChartClick = () => false;
          persistViewState = () => {};
          renderPanel = () => {};
          renderCharts = () => {};
          scheduleViewPanelRender = () => {};
          maybeLoadMoreHistory = () => {};
          updateChartNavigationControls = () => {};
          dockFixture.state.snapshot = { bars: [bar] };
          dockFixture.state.drawing.tool = 'cursor';
          dockFixture.state.drawing.draft = null;
          dockFixture.state.paperTrading.armed = false;
          setupChartInteractions();
        }"""
    )

    page.dispatch_event(
        "#price-chart",
        "pointerdown",
        {
            "pointerId": 71,
            "pointerType": "touch",
            "button": 0,
            "clientX": 120,
            "clientY": 180,
        },
    )
    page.wait_for_timeout(560)
    assert page.evaluate("window.touchContextRequests") == [
        {"id": "price-chart", "x": 120, "y": 180, "pointerType": "touch"}
    ]

    page.dispatch_event(
        "#price-chart",
        "pointerdown",
        {
            "pointerId": 72,
            "pointerType": "touch",
            "button": 0,
            "clientX": 120,
            "clientY": 180,
        },
    )
    page.dispatch_event(
        "#price-chart",
        "pointermove",
        {
            "pointerId": 72,
            "pointerType": "touch",
            "button": 0,
            "clientX": 140,
            "clientY": 180,
        },
    )
    page.wait_for_timeout(560)
    assert len(page.evaluate("window.touchContextRequests")) == 1


@pytest.mark.parametrize("reason", ["dock", "overflow"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_lens_preserves_header_and_row_tooltips_at_minimum_width(
    dock_page: Page, reason: str, side: str
) -> None:
    page = dock_page
    _load_lens_tooltips(page, reason)
    page.locator("#settings-toggle").click()
    page.locator("#workspace-dock-side").select_option(side)
    page.locator("#settings-close").click()
    page.locator("#indicator-lens-toggle").click()
    page.locator("#workspace-dock-resizer").press("Home")

    tooltip = page.locator("#canvas-tooltip")
    page.locator(".indicator-lens-card-head span").hover()
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("TABLE DETAILS")
    expect(tooltip.locator(".tooltip-positive")).to_contain_text("CALL table context")
    expect(tooltip.locator(".tooltip-call")).to_have_text("CALL")
    expect(tooltip.locator(".tooltip-table")).to_contain_text("Reference")
    expect(tooltip.locator(".tooltip-table")).to_contain_text("123.45")
    box = _box(page, "#canvas-tooltip")
    assert box["x"] >= 10
    assert box["x"] + box["width"] <= 1430
    assert box["y"] >= 10
    assert box["y"] + box["height"] <= 890

    row = page.locator(".indicator-lens-row").first
    for cell in row.locator(".indicator-lens-cell").all():
        cell.hover()
        expect(tooltip).to_contain_text("ROW DETAILS")
        expect(tooltip).not_to_contain_text("TABLE DETAILS")
        expect(tooltip.locator(".tooltip-negative")).to_contain_text("PUT row context")
        expect(tooltip.locator(".tooltip-put")).to_have_text("PUT")
    row.click()
    assert page.evaluate("window.fixtureClickedSurface") == "indicator-lens"

    page.locator(".indicator-lens-row").nth(1).hover()
    expect(tooltip).to_contain_text("TABLE DETAILS")
    expect(tooltip).not_to_contain_text("ROW DETAILS")
    page.locator("#price-frame").hover()
    expect(tooltip).to_be_hidden()


def test_lens_tooltip_tracks_current_data_and_clears_with_its_surface(dock_page: Page) -> None:
    page = dock_page
    _load_lens_tooltips(page)
    page.locator("#indicator-lens-toggle").click()
    tooltip = page.locator("#canvas-tooltip")
    page.locator(".indicator-lens-card-head").hover()
    expect(tooltip).to_contain_text("CALL table context")
    assert page.evaluate("""() => {
      const head = document.querySelector('.indicator-lens-card-head');
      const entries = structuredClone(window.fixtureTooltipEntries);
      entries[0].overlay.table.tooltip_sections[0].lines[0].text = 'Fresh table context';
      updateIndicatorLensTables(entries);
      return head === document.querySelector('.indicator-lens-card-head');
    }""")
    expect(tooltip).to_contain_text("Fresh table context")
    expect(tooltip).not_to_contain_text("CALL table context")

    page.locator(".indicator-lens-row").first.hover()
    page.evaluate("""() => {
      const entries = structuredClone(window.fixtureTooltipEntries);
      const column = entries[0].overlay.table.columns[0];
      column.cells[1] = 'Updated value';
      column.tooltip_sections[0].lines[0].text = 'Fresh row context';
      updateIndicatorLensTables(entries);
    }""")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("Updated value")
    expect(tooltip).to_contain_text("Fresh row context")
    expect(tooltip).not_to_contain_text("PUT row context")

    page.evaluate("openWorkspaceDockSurface('option-drive')")
    expect(tooltip).to_be_hidden()
    page.locator("#indicator-lens-toggle").click()
    page.locator(".indicator-lens-row").first.hover()
    expect(tooltip).to_be_visible()
    page.evaluate("closeWorkspaceDockSurface('indicator-lens')")
    expect(tooltip).to_be_hidden()
    page.locator("#indicator-lens-toggle").click()
    page.evaluate("""() => {
      const entries = structuredClone(window.fixtureTooltipEntries);
      const column = entries[0].overlay.table.columns[0];
      entries[0].overlay.table.columns = Array.from({length: 40}, () => column);
      updateIndicatorLensTables(entries);
    }""")
    page.locator(".indicator-lens-row").first.hover()
    expect(tooltip).to_be_visible()
    page.locator("#indicator-lens-tables").evaluate("node => { node.scrollTop = 100; }")
    expect(tooltip).to_be_hidden()
    page.locator(".indicator-lens-row").nth(10).hover()
    expect(tooltip).to_be_visible()
    page.evaluate("updateIndicatorLensTables([])")
    expect(tooltip).to_be_hidden()

    # A background lens update must not dismiss another surface's tooltip.
    page.evaluate("""() => {
      showCanvasTooltip(100, 100, 'Chart tooltip');
      updateIndicatorLensTables([]);
    }""")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_have_text("Chart tooltip")


def test_dock_resize_side_and_surface_lifecycle(dock_page: Page) -> None:
    page = dock_page
    page.locator("#indicator-lens-toggle").click()
    expect(page.locator("#indicator-lens-surface")).to_be_visible()
    expect(page.locator("#workspace-dock-resizer")).to_have_attribute("aria-valuenow", "300")
    assert _box(page, "#workspace-dock")["width"] == 300
    chart = _box(page, ".chart-panel")
    dock = _box(page, "#workspace-dock")
    assert dock["x"] == chart["x"] + chart["width"]

    page.locator("#workspace-dock-resizer").press("Home")
    expect(page.locator("#workspace-dock-resizer")).to_have_attribute("aria-valuenow", "220")
    assert _box(page, "#workspace-dock")["width"] == 220
    assert page.locator(".indicator-lens-row").evaluate_all(
        "rows => rows.every(row => row.scrollWidth <= row.clientWidth)"
    )
    page.locator("#workspace-dock-resizer").press("ArrowLeft")
    expect(page.locator("#workspace-dock-resizer")).to_have_attribute("aria-valuenow", "228")

    page.locator("#settings-toggle").click()
    page.locator("#workspace-dock-side").select_option("left")
    expect(page.locator("body")).to_have_class(re.compile("workspace-dock-left"))
    page.locator("#settings-close").click()
    chart = _box(page, ".chart-panel")
    dock = _box(page, "#workspace-dock")
    assert dock["x"] + dock["width"] == chart["x"]
    page.locator("#workspace-dock-resizer").press("ArrowRight")
    expect(page.locator("#workspace-dock-resizer")).to_have_attribute("aria-valuenow", "236")
    handle = _box(page, "#workspace-dock-resizer")
    page.mouse.move(handle["x"] + 2, 450)
    page.mouse.down()
    page.mouse.move(handle["x"] + 66, 450, steps=4)
    page.mouse.up()
    expect(page.locator("#workspace-dock-resizer")).to_have_attribute("aria-valuenow", "300")
    assert page.evaluate("dockFixture.writes.at(-1)") == ["aef:workspaceDockWidth", "300"]

    page.locator("#option-drive-toggle").click()
    expect(page.locator("#option-drive")).to_be_visible()
    expect(page.locator(".option-drive-paper-badge")).to_be_visible()
    assert page.evaluate("currentOptionDriveState().open")
    page.evaluate("currentOptionDriveState().armed = true")
    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-surface")).to_be_visible()
    expect(page.locator("#gex-dock-profile #gex-sidebar-chrome")).to_be_visible()
    assert not page.evaluate("currentOptionDriveState().armed || currentOptionDriveState().open")
    assert not page.evaluate("gexSidebarShellVisible()")
    assert page.evaluate("layout.gexSidebarPlacement") == "chart"
    assert (
        page.evaluate("dockFixture.writes.filter(([key]) => key === 'aef:gexSidebarPlacement')")
        == []
    )

    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-surface")).to_be_hidden()
    assert _box(page, "#workspace-dock")["width"] == 34

    expect(page.locator("#price-frame #gex-sidebar-chrome")).to_be_visible()
    assert page.evaluate("layout.gexSidebarPlacement") == "chart"


@pytest.mark.parametrize("previous", ["chart", "dock", "disabled"])
@pytest.mark.parametrize(
    "close_button", ["gex-dock-toggle", "indicator-lens-toggle", "option-drive-toggle"]
)
def test_gex_tab_restores_previous_sidebar_on_close_or_switch(
    dock_page: Page, previous: str, close_button: str
) -> None:
    page = dock_page
    saved_placement = "dock" if previous == "dock" else "chart"
    page.evaluate(
        """({placement, enabled}) => {
          layout.gexSidebarPlacement = placement;
          layout.gexSidebarWidth = 333;
          state.indicators.gexContext.enabled = enabled;
          applyLayout();
        }""",
        {"placement": saved_placement, "enabled": previous != "disabled"},
    )
    before = page.evaluate("""() => ({
      width: layout.gexSidebarWidth, config: state.indicators.gexContext,
      gutter: gexProfileGutterGeometry({left: 8, right: 70}, 700).effectiveWidth,
    })""")
    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-profile #gex-sidebar-chrome")).to_be_visible()
    assert page.evaluate("gexSidebarShellVisible()") is False
    assert page.evaluate("layout.gexSidebarPlacement") == saved_placement

    page.locator(f"#{close_button}").click()
    expect(page.locator("#gex-dock-surface")).to_be_hidden()
    host = "gex-dock-profile" if saved_placement == "dock" else "price-frame"
    expect(page.locator(f"#{host} #gex-sidebar-chrome")).to_have_count(1)
    assert page.evaluate("gexSidebarShellVisible()") is (previous == "chart")
    assert page.evaluate("layout.gexSidebarPlacement") == saved_placement
    assert (
        page.evaluate("""() => ({
      width: layout.gexSidebarWidth, config: state.indicators.gexContext,
      gutter: gexProfileGutterGeometry({left: 8, right: 70}, 700).effectiveWidth,
    })""")
        == before
    )
    assert page.evaluate("dockFixture.writes") == []


def test_gex_explicit_placement_setting_remains_durable(dock_page: Page) -> None:
    page = dock_page
    page.locator("#settings-toggle").click()
    page.locator("#gex-sidebar-placement").select_option("dock")
    page.locator("#settings-close").click()
    expect(page.locator("#gex-dock-profile #gex-sidebar-chrome")).to_be_visible()
    assert page.evaluate("dockFixture.writes.at(-1)") == ["aef:gexSidebarPlacement", "dock"]
    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-surface")).to_be_hidden()
    expect(page.locator("#gex-dock-profile #gex-sidebar-chrome")).to_have_count(1)
    assert page.evaluate("layout.gexSidebarPlacement") == "dock"

    page.locator("#gex-dock-toggle").click()
    page.locator("#settings-toggle").click()
    page.evaluate("dockFixture.rejectWrite(true)")
    page.locator("#gex-sidebar-placement").select_option("chart")
    expect(page.locator("#gex-sidebar-placement")).to_have_value("dock")
    assert page.evaluate("workspaceDockSurfaceIsActive('gex')") is True
    page.evaluate("dockFixture.rejectWrite(false)")
    page.locator("#gex-sidebar-placement").select_option("chart")
    page.locator("#settings-close").click()
    expect(page.locator("#gex-dock-surface")).to_be_hidden()
    expect(page.locator("#price-frame #gex-sidebar-chrome")).to_be_visible()
    assert page.evaluate("dockFixture.writes") == [
        ["aef:gexSidebarPlacement", "dock"],
        ["aef:gexSidebarPlacement", "chart"],
    ]


def test_table_geometry_excludes_gex_and_routes_width_overflow(dock_page: Page) -> None:
    result = dock_page.evaluate("""() => {
      state.indicators.gexContext.enabled = true;
      const pad = {left: 8, right: 70, top: 20};
      const overlay = {source: 'fixture', table: {columns: [['A'], ['B'], ['C']]}};
      const placements = ['top', 'bottom', 'top-left', 'top-right', 'bottom-left', 'bottom-right'];
      const rects = [];
      const ctx = document.createElement('canvas').getContext('2d');
      ctx.fillRect = (x, y, w, h) => rects.push({x, y, w, h});
      const chartPad = indicatorTablePlacementPad(pad, 700);
      for (const position of placements) drawGenericIndicatorTable(ctx, overlay, pad, 700, 500, 0, position);
      const chartFits = indicatorTableWidthLayout(overlay, 'top', chartPad, 520).fits;
      layout.gexSidebarPlacement = 'dock';
      const dockPad = indicatorTablePlacementPad(pad, 520);
      const dockFits = indicatorTableWidthLayout(overlay, 'top', dockPad, 520).fits;
      return {rects, chartPad, chartFits, dockPad, dockFits};
    }""")
    assert result["rects"]
    assert all(rect["x"] >= result["chartPad"]["left"] for rect in result["rects"])
    assert all(rect["x"] + rect["w"] <= 630 for rect in result["rects"])
    assert result["chartFits"] is False
    assert result["dockFits"] is True
    assert result["dockPad"]["left"] == 8


@pytest.mark.parametrize("side", ["left", "right"])
def test_requested_dock_width_preserves_chart_and_sidebar_on_small_screen(
    dock_page: Page, side: str
) -> None:
    page = dock_page
    page.set_viewport_size({"width": 900, "height": 720})
    page.evaluate(
        """side => {
      layout.workspaceDockWidth = 640;
      layout.workspaceDockSide = side;
      document.body.classList.add('ui-flat', 'view-preset-mobile');
      applyLayout();
    }""",
        side,
    )
    page.locator("#indicator-lens-toggle").click()
    expect(page.locator("#indicator-lens-surface")).to_be_visible()
    assert _box(page, ".chart-panel")["width"] >= 320
    dock = _box(page, "#workspace-dock")
    assert 220 <= dock["width"] < 640
    sidebar = _box(page, ".side-panel")
    assert sidebar["x"] + sidebar["width"] <= 900
    assert page.evaluate("layout.workspaceDockWidth") == 640


def test_dock_rejected_settings_do_not_block_temporary_gex_tab(dock_page: Page) -> None:
    page = dock_page
    page.evaluate("dockFixture.rejectWrite(true)")
    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-profile #gex-sidebar-chrome")).to_be_visible()
    assert page.evaluate("layout.gexSidebarPlacement") == "chart"
    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-surface")).to_be_hidden()
    expect(page.locator("#price-frame #gex-sidebar-chrome")).to_be_visible()
    assert page.evaluate("layout.gexSidebarPlacement") == "chart"
    page.locator("#settings-toggle").click()
    page.locator("#gex-sidebar-placement").select_option("dock")
    expect(page.locator("#gex-sidebar-placement")).to_have_value("chart")
    assert page.evaluate("workspaceDockSurfaceIsActive('gex')") is False
    page.locator("#workspace-dock-side").select_option("left")
    expect(page.locator("#workspace-dock-side")).to_have_value("right")
    page.locator("#settings-close").click()
    page.locator("#indicator-lens-toggle").click()
    page.locator("#workspace-dock-resizer").press("Home")
    expect(page.locator("#workspace-dock-resizer")).to_have_attribute("aria-valuenow", "300")
    assert page.evaluate("dockFixture.writes") == []


def test_gex_dock_uses_primary_price_projection_and_own_tooltip_coordinates(
    dock_page: Page,
) -> None:
    page = dock_page
    page.locator("#gex-dock-toggle").click()
    expect(page.locator("#gex-dock-profile #gex-sidebar-chrome")).to_be_visible()
    result = page.evaluate("""() => {
      const original = drawGexProfileCompanion;
      let projected;
      // Substitute only data admission/orchestration; use the actual profile renderer.
      drawGexProfileCompanion = (ctx, snapshot, visible, pad, priceH, width, y, options) => {
        projected = {y: y(100), width, tooltipChart: options.tooltipChart};
        drawGexSplitProfile(ctx, {}, [{strike: 100, profile_y: y(100), profile_ratio: 0.8,
          call_gex: 200000, put_gex: -100000, net_gex: 100000}], snapshot, visible,
          pad, priceH, width, y, 'GEX fixture', [], options);
      };
      try {
        const geometry = {visible: {bars: []}, pad: {left: 8, top: 20, right: 70},
          priceH: 500, y: price => 450 - price * 2};
        renderGexDockFromGeometry({bars: [{}]}, geometry);
        const canvas = document.getElementById('gex-dock-canvas');
        const rect = canvas.getBoundingClientRect();
        const priceRect = document.getElementById('price-frame').getBoundingClientRect();
        const tooltip = canvasTooltipHit('gex-dock', rect.width / 2, 250);
        const beforeClear = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data.some(value => value > 0);
        const priceHits = state.tooltip.price.length;
        renderGexDockFromGeometry(null, null);
        const afterClear = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data.some(value => value > 0);
        return {projected, top: rect.top, priceTop: priceRect.top, hitY: tooltip?.y1,
          priceHits, beforeClear, afterClear, hitsAfterClear: state.tooltip['gex-dock'].length};
      } finally { drawGexProfileCompanion = original; }
    }""")
    assert result["projected"]["y"] == 250
    assert result["projected"]["tooltipChart"] == "gex-dock"
    assert result["top"] == result["priceTop"]
    assert result["hitY"] == 241
    assert result["priceHits"] == 0
    assert result["beforeClear"] is True
    assert result["afterClear"] is False
    assert result["hitsAfterClear"] == 0
