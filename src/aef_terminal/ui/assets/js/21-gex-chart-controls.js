    function gexSidebarIsDocked() {
      return workspaceDockSurfaceIsActive("gex") || layout.gexSidebarPlacement === "dock";
    }

    function syncGexSidebarHost() {
      const chrome = document.getElementById("gex-sidebar-chrome");
      const host = document.getElementById(gexSidebarIsDocked() ? "gex-dock-profile" : "price-frame");
      if (chrome && host && chrome.parentElement !== host) host.appendChild(chrome);
      if (gexSidebarIsDocked() && host && workspaceDockSurfaceIsActive("gex")) {
        const priceRect = document.getElementById("price-frame").getBoundingClientRect();
        host.style.top = `${priceRect.top - host.parentElement.getBoundingClientRect().top}px`;
        host.style.height = `${priceRect.height}px`;
      }
      const input = document.getElementById("gex-sidebar-placement");
      if (input) input.value = layout.gexSidebarPlacement;
      renderGexSidebarChrome();
    }

    function setupGexDock() {
      registerWorkspaceDockSurface("gex");
      document.getElementById("gex-dock-toggle")?.addEventListener("click", () => {
        // Opening the tab borrows the sidebar without changing its saved placement.
        toggleWorkspaceDockSurface("gex");
      });
      document.getElementById("gex-sidebar-placement")?.addEventListener("change", event => {
        const placement = event.target.value;
        if (!setServerSettingValue("aef:gexSidebarPlacement", placement)) {
          refreshWorkspaceDockLayout();
          return;
        }
        layout.gexSidebarPlacement = placement;
        if (placement === "dock") openWorkspaceDockSurface("gex");
        else closeWorkspaceDockSurface("gex");
        refreshWorkspaceDockLayout();
      });
      const canvas = document.getElementById("gex-dock-canvas");
      canvas?.addEventListener("pointermove", event => {
        const rect = canvas.getBoundingClientRect();
        updateCanvasTooltip(canvas.id, event.clientX - rect.left, event.clientY - rect.top, event.clientX, event.clientY);
      });
      canvas?.addEventListener("pointerleave", hideCanvasTooltip);
    }

    function clearGexContext() {
      if (typeof clearGexOptionUniverseExpiryTimer === "function") {
        clearGexOptionUniverseExpiryTimer();
      }
      state.gexContext = null;
      if (state.snapshot?.gex) delete state.snapshot.gex;
      renderGexDockFromGeometry(null, null);
      renderGexSidebarChrome(null);
    }

    function gexContextModeButtonLabel(gex = activeGexContext(state.snapshot) || gexStatusContext(state.snapshot) || state.gexContext || null) {
      const requestedLive = typeof gexLiveModeRequested === "function"
        ? gexLiveModeRequested()
        : String(state.indicators?.gexContext?.mode || "request") === "live";
      const mode = typeof effectiveGexContextMode === "function"
        ? effectiveGexContextMode()
        : String(state.indicators?.gexContext?.mode || "request");
      const blockReason = typeof gexLiveBlockReason === "function" ? gexLiveBlockReason() : "";
      const blocked = requestedLive && (mode !== "live" || Boolean(blockReason));
      const modeText = blocked ? "STRM BLK" : mode === "live" ? "STRM" : "RQST";
      const age = gex && typeof gexSnapshotAgeText === "function" ? gexSnapshotAgeText(gex) : "";
      return age && age !== "no time" ? `${modeText} DATA ${age}` : modeText;
    }

    function gexContextModeButtonTitle(gex = activeGexContext(state.snapshot) || gexStatusContext(state.snapshot) || state.gexContext || null) {
      const requested = String(state.indicators?.gexContext?.mode || "request") === "live" ? "live" : "request";
      const mode = typeof effectiveGexContextMode === "function" ? effectiveGexContextMode() : requested;
      const requestedLabel = requested === "live" ? "STREAM" : "REQUEST";
      const modeLabel = mode === "live" ? "STREAM" : "REQUEST";
      const blockReason = typeof gexLiveBlockReason === "function" ? gexLiveBlockReason() : "";
      const refreshStatus = String(gex?.refresh_status || gex?.refresh_request?.status || "").trim().toUpperCase();
      const refreshAge = gex?.refresh_finished_at && typeof gexSnapshotAgeText === "function"
        ? gexSnapshotAgeText({ captured_at: gex.refresh_finished_at })
        : "";
      const lines = [
        `Switch GEX capture cadence: requested ${requestedLabel} · effective ${modeLabel}`,
        gex ? `Broker market data entitlement: ${gexMarketDataEntitlementLabel(gex)}` : "",
        gex && typeof gexSnapshotAgeText === "function" ? `Data age: ${gexSnapshotAgeText(gex)}` : "",
        refreshStatus ? `Last request: ${refreshStatus}${refreshAge && refreshAge !== "no time" ? ` · ${refreshAge} ago` : ""}` : "",
        blockReason ? `STREAM blocked: ${blockReason}` : "",
        gex && typeof gexGlobalRegimeText === "function" ? gexGlobalRegimeText(gex) : "",
        gex && typeof gexFreshnessText === "function" ? gexFreshnessText(gex) : "",
        typeof gex?.message === "string" ? gex.message : "",
      ];
      return lines.filter(Boolean).join("\n");
    }

    function gexSidebarStatusPresentation(gex, options = {}) {
      const requestLoading = Boolean(
        state.loadingGex
        || gex?.refresh_running === true
        || gex?.refresh_queued === true
        || gex?.status === "loading"
      );
      if (!gex) {
        return {
          text: requestLoading ? "GEX · loading" : "GEX · waiting",
          title: requestLoading
            ? "Waiting for the exact provider-qualified GEX context."
            : "GEX context has not been loaded for the active route.",
          tone: "warn",
        };
      }
      const hasDisplayData = typeof gexContextHasDisplayData === "function"
        ? gexContextHasDisplayData(gex)
        : Boolean(Array.isArray(gex.levels) && gex.levels.length);
      const globalGammaRegime = typeof gex.global_gamma_regime === "string"
        ? gex.global_gamma_regime
        : "UNKNOWN";
      const regime = !hasDisplayData
        ? "GEX"
        : gex.preserved_context
          ? globalGammaRegime === "POSITIVE_ESTIMATE"
            ? "G+ HIST"
            : globalGammaRegime === "NEGATIVE_ESTIMATE"
              ? "G- HIST"
              : "GEX? HIST"
          : globalGammaRegime === "POSITIVE_ESTIMATE"
            ? "G+ EST"
            : globalGammaRegime === "NEGATIVE_ESTIMATE"
              ? "G- EST"
              : "GEX?";
      const entitlement = hasDisplayData ? ` · ${gexMarketDataEntitlementLabel(gex)}` : "";
      const statusLabel = requestLoading ? "loading" : String(gex.status || "cache");
      const text = `${regime}${entitlement} · ${statusLabel}`;
      const statusWarn = typeof gexStatusWarn === "function"
        ? gexStatusWarn(String(gex.status || ""))
        : ["stale", "error", "timeout", "partial"].includes(String(gex.status || "").toLowerCase());
      const tone = statusWarn || !["POSITIVE_ESTIMATE", "NEGATIVE_ESTIMATE"].includes(globalGammaRegime)
        ? "warn"
        : globalGammaRegime === "NEGATIVE_ESTIMATE"
          ? "negative"
          : "positive";
      const titleLines = [
        text,
        typeof gexGlobalRegimeText === "function" ? gexGlobalRegimeText(gex) : "",
        typeof gexFreshnessText === "function" ? gexFreshnessText(gex) : "",
        typeof gexComparisonScopeText === "function" ? gexComparisonScopeText(gex) : "",
        "Model: Call GEX positive / Put GEX negative inventory convention; dealer inventory is not observed.",
        typeof gex?.message === "string" ? gex.message : "",
      ];
      if (options.includeDiagnostics) {
        const visibility = typeof gexLevelVisibilitySummary === "function"
          ? gexLevelVisibilitySummary(gex.levels || [], state.indicators?.gexContext || {})
          : null;
        const diagnostics = typeof gexStatusDiagnosticLines === "function"
          ? gexStatusDiagnosticLines(gex, visibility).join("\n")
          : "";
        if (diagnostics) titleLines.push(diagnostics);
      }
      return { text, title: titleLines.filter(Boolean).join("\n"), tone };
    }

    function renderGexSidebarRefreshButton(gex) {
      const button = document.getElementById("gex-sidebar-refresh");
      if (!button) return;
      const refreshDisabled = typeof gexLiveModeActive === "function" && gexLiveModeActive();
      const requestLoading = Boolean(
        state.loadingGex
        || gex?.refresh_running === true
        || gex?.refresh_queued === true
        || gex?.status === "loading"
      );
      const refreshState = state.gexManualRefresh || {};
      const refreshKey = typeof gexContextKey === "function" ? gexContextKey() : "";
      const manualScopeMatches = Boolean(!refreshKey || refreshState.requestKey === refreshKey);
      const manualActive = Boolean(refreshState.active && manualScopeMatches);
      const recentStatus = manualScopeMatches
        && !manualActive
        && Date.now() - Number(refreshState.finishedAt || 0) < 2600
        ? String(refreshState.status || "")
        : "";
      const loading = !refreshDisabled && (requestLoading || manualActive);
      const statusError = recentStatus === "unavailable"
        || ["blocked", "cancelled", "error", "rejected"].includes(recentStatus);
      const statusOk = recentStatus === "published";
      button.classList.toggle("loading", loading);
      button.classList.toggle("warn", statusError);
      button.classList.toggle("ok", statusOk);
      const disabled = Boolean(state.serverSleeping || refreshDisabled || loading);
      if (button.disabled !== disabled) button.disabled = disabled;
      const text = refreshDisabled ? "—" : loading ? "…" : statusError ? "!" : statusOk ? "OK" : "↻";
      if (button.textContent !== text) button.textContent = text;
      const title = refreshDisabled
        ? "Manual option-chain refresh is available in REQUEST cadence; STREAM cadence uses its exact broker session."
        : loading
          ? refreshState.message || "GEX refresh is running"
          : statusError
            ? refreshState.message || "GEX refresh failed"
            : statusOk
            ? refreshState.message || "GEX refresh finished"
              : "Refresh GEX option-chain levels";
      if (button.title !== title) button.title = title;
      if (button.getAttribute("aria-label") !== title) {
        button.setAttribute("aria-label", title);
      }
    }

    function gexZoneMode(value) {
      const mode = String(value || state.indicators?.gexContext?.zoneStyle || "");
      if (["off", "zones", "lines", "strike"].includes(mode)) return mode;
      return state.indicators?.gexContext?.zones ? "zones" : "off";
    }

    function gexHistoryView(value) {
      const mode = String(value || state.indicators?.gexContext?.historyView || "");
      return GEX_HISTORY_VIEW_MODES.includes(mode) ? mode : "last";
    }

    function storedGexHistoryView(instrumentId) {
      const stored = storedInstrumentIndicatorString(
        "gexContextHistoryView",
        "last",
        instrumentId,
      );
      return GEX_HISTORY_VIEW_MODES.includes(stored) ? stored : "last";
    }

    function saveGexHistoryView(value) {
      saveInstrumentIndicatorSetting("gexContextHistoryView", gexHistoryView(value));
    }

    function renderGexViewMenu() {
      const menu = document.getElementById("gex-view-menu");
      if (!menu) return;
      const activeMode = state.indicators?.gexContext?.profile
        ? gexProfileMode(state.indicators?.gexContext?.profileStyle)
        : "off";
      menu.querySelectorAll("[data-gex-view-mode]").forEach(button => {
        const active = button.dataset.gexViewMode === activeMode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function renderGexZoneMenu() {
      const menu = document.getElementById("gex-zone-menu");
      if (!menu) return;
      const activeMode = gexZoneMode(state.indicators?.gexContext?.zoneStyle);
      menu.querySelectorAll("[data-gex-zone-mode]").forEach(button => {
        const active = button.dataset.gexZoneMode === activeMode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function renderGexHistoryMenu() {
      const menu = document.getElementById("gex-history-menu");
      if (!menu) return;
      const activeView = gexHistoryView(state.indicators?.gexContext?.historyView);
      menu.querySelectorAll("[data-gex-history-view]").forEach(button => {
        const active = button.dataset.gexHistoryView === activeView;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function gexSidebarSettingsOpen() {
      return document.getElementById("gex-sidebar-settings-toggle")?.getAttribute("aria-expanded") === "true";
    }

    function renderGexSidebarSettings(openOverride = null) {
      const panel = document.getElementById("gex-sidebar-settings");
      const toggle = document.getElementById("gex-sidebar-settings-toggle");
      if (!panel || !toggle) return;
      const open = openOverride === null ? gexSidebarSettingsOpen() : Boolean(openOverride);
      panel.classList.toggle("hidden", !open);
      toggle.classList.toggle("active", open);
      const expanded = open ? "true" : "false";
      if (toggle.getAttribute("aria-expanded") !== expanded) {
        toggle.setAttribute("aria-expanded", expanded);
      }
      if (open) {
        const contextToggle = document.getElementById("gex-context-toggle");
        const schedulerToggle = document.getElementById("gex-scheduler-toggle");
        const dynamicsToggle = document.getElementById("gex-dynamics-toggle");
        if (contextToggle) contextToggle.checked = Boolean(state.indicators?.gexContext?.enabled);
        if (schedulerToggle) schedulerToggle.checked = Boolean(state.settings?.gexSchedulerEnabled);
        if (dynamicsToggle) {
          dynamicsToggle.checked = Boolean(state.indicators?.gexContext?.dynamicsVisible);
        }
        renderGexViewMenu();
        renderGexZoneMenu();
        renderGexHistoryMenu();
      }
    }

    function renderGexSidebarChrome(gexOverride = undefined, visibleOverride = null) {
      const chrome = document.getElementById("gex-sidebar-chrome");
      if (!chrome) return;
      const layerVisible = visibleOverride === null
        ? (typeof gexLayerVisible === "function"
            ? gexLayerVisible()
            : Boolean(state.indicators?.gexContext?.enabled))
        : Boolean(visibleOverride);
      chrome.classList.remove("hidden");
      chrome.classList.toggle("inactive", !layerVisible);
      document.getElementById("price-frame")?.classList.toggle(
        "gex-sidebar-visible",
        layerVisible && !gexSidebarIsDocked(),
      );
      const gex = gexOverride === undefined
        ? activeGexContext(state.snapshot) || gexStatusContext(state.snapshot) || state.gexContext || null
        : gexOverride;
      const status = layerVisible
        ? gexSidebarStatusPresentation(gex)
        : {
            text: "GEX",
            title: "GEX context is off. Open settings to enable it.",
            tone: "warn",
          };
      const statusNode = document.getElementById("gex-sidebar-status");
      if (statusNode) {
        if (statusNode.textContent !== status.text) statusNode.textContent = status.text;
        if (!statusNode.matches(":hover") && statusNode.title !== status.title) {
          statusNode.title = status.title;
        }
        statusNode.classList.toggle("positive", status.tone === "positive");
        statusNode.classList.toggle("negative", status.tone === "negative");
        statusNode.classList.toggle("warn", status.tone === "warn");
      }
      const modeButton = document.getElementById("gex-context-mode-toggle");
      if (modeButton) {
        const requestedLive = typeof gexLiveModeRequested === "function"
          ? gexLiveModeRequested()
          : String(state.indicators?.gexContext?.mode || "request") === "live";
        const mode = typeof effectiveGexContextMode === "function"
          ? effectiveGexContextMode()
          : String(state.indicators?.gexContext?.mode || "request");
        const blockReason = typeof gexLiveBlockReason === "function" ? gexLiveBlockReason() : "";
        const blocked = requestedLive && (mode !== "live" || Boolean(blockReason));
        const warn = status.tone === "warn";
        modeButton.classList.toggle("active", requestedLive);
        modeButton.classList.toggle("live", mode === "live");
        modeButton.classList.toggle("request", !requestedLive);
        modeButton.classList.toggle("blocked", blocked);
        modeButton.classList.toggle("warn", warn || blocked);
        const modeText = gexContextModeButtonLabel(gex);
        if (modeButton.textContent !== modeText) modeButton.textContent = modeText;
        const modeTitle = gexContextModeButtonTitle(gex);
        if (modeButton.title !== modeTitle) modeButton.title = modeTitle;
        const modeDisabled = Boolean(state.serverSleeping);
        if (modeButton.disabled !== modeDisabled) modeButton.disabled = modeDisabled;
      }
      renderGexSidebarRefreshButton(gex);
      renderGexSidebarSettings();
    }

    function renderGexContextModeButton(visibleOverride = null) {
      renderGexSidebarChrome(undefined, visibleOverride);
    }
