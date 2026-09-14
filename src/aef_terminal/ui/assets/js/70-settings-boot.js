    function ema233AlertRearmSetting(value) {
      const minutes = Number(value);
      return [15, 30, 60].includes(minutes) ? minutes : 60;
    }

    function selectedSettingsOptionLabel(id, fallback = "") {
      const select = document.getElementById(id);
      return String(select?.selectedOptions?.[0]?.textContent || fallback).trim();
    }

    function renderSettingsGroupSummaries() {
      const setSummary = (id, parts) => {
        const node = document.getElementById(id);
        if (node) node.textContent = parts.filter(Boolean).join(" · ");
      };
      setSummary("settings-interface-summary", [
        selectedSettingsOptionLabel("ui-shape-select", "Interface"),
        selectedSettingsOptionLabel("view-preset-select", "Trading"),
      ]);
      setSummary("settings-signals-summary", [
        selectedSettingsOptionLabel("strategy-mode-select", "Balanced"),
        selectedSettingsOptionLabel("signals-range-select", "2 days"),
        selectedSettingsOptionLabel("signal-density-select", "Focus"),
      ]);
      setSummary("settings-paper-summary", [
        `R:R ${selectedSettingsOptionLabel("paper-min-rr-select", "1.25")}`,
        document.getElementById("paper-edge-gate-toggle")?.checked ? "Edge block" : "Edge pass",
      ]);
    }

    function applySettings() {
      const ui = applySettings._nodes || (applySettings._nodes = {});
      const el = id => {
        const cached = ui[id];
        if (cached && cached.isConnected) return cached;
        return ui[id] = document.getElementById(id);
      };
      const syncManifestIndicatorControlInputs = () => {
        Object.entries(indicatorRegistry()).forEach(([, spec]) => {
          const uiSpec = spec?.ui || {};
          const group = state.indicators?.[uiSpec.state_key];
          if (!group) return;
          (spec.controls || []).forEach(control => {
            const node = document.getElementById(control.element_id);
            if (!node) return;
            const key = control.state_key || control.key;
            const fallback = control.default;
            const value = group[key] ?? fallback;
            if (control.control_type === "toggle") {
              node.checked = Boolean(value);
              return;
            }
            if (control.control_type === "number") {
              const minimum = Number(control.minimum);
              const maximum = Number(control.maximum);
              const low = Number.isFinite(minimum) ? minimum : -Infinity;
              const high = Number.isFinite(maximum) ? maximum : Infinity;
              const numeric = Number(value);
              const fallbackNumeric = Number(fallback);
              node.value = String(clamp(Number.isFinite(numeric) ? numeric : fallbackNumeric, low, high));
              return;
            }
            const options = Array.isArray(control.options) ? control.options : [];
            node.value = options.length && !options.includes(value) ? String(fallback ?? "") : String(value ?? fallback ?? "");
          });
        });
      };
      document.body.classList.toggle("theme-light", state.settings.theme === "light");
      if (window.mcTelemetrySet) window.mcTelemetrySet("theme", state.settings.theme);
      const uiShape = normalizeUiShape(state.settings.uiShape);
      state.settings.uiShape = uiShape;
      const isTvPalette = uiShape === "tv-flat" || uiShape === "tv-black" || uiShape === "tv-black-flat";
      const isBlackPalette = uiShape === "tv-black" || uiShape === "tv-black-flat";
      const isFlatShape = uiShape === "flat" || uiShape === "tv-flat" || uiShape === "tv-black-flat";
      document.body.classList.toggle("ui-flat", isFlatShape);
      document.body.classList.toggle("ui-tv-flat", isTvPalette);
      document.body.classList.toggle("ui-tv-black", isBlackPalette);
      document.body.classList.toggle("ui-rounded", !isFlatShape);
      const motionPreference = normalizeMotionPreference(state.settings.motionPreference);
      state.settings.motionPreference = motionPreference;
      document.body.classList.toggle("ui-motion-reduced", motionPreference === "reduced");
      document.body.classList.toggle("ui-motion-off", motionPreference === "off");
      refreshThemeColorCache();
      if (typeof mtfLensApplyStoredPresentation === "function") {
        mtfLensApplyStoredPresentation();
      }
      document.body.classList.toggle("gex-focus-mode", gexFocusMode());
      const currentViewPreset = normalizeViewPreset(state.settings.viewPreset);
      const currentViewBase = currentViewPreset.startsWith("gex") ? "gex" : currentViewPreset;
      for (const preset of ["trading", "gex", "alerts", "mobile"]) {
        document.body.classList.toggle(`view-preset-${preset}`, currentViewBase === preset);
      }
      el("settings-panel").classList.toggle("open", state.settings.open);
      el("settings-toggle").classList.toggle("active", state.settings.open);
      el("settings-panel").setAttribute("aria-hidden", state.settings.open ? "false" : "true");
      el("settings-toggle").setAttribute("aria-expanded", state.settings.open ? "true" : "false");
      const themeToggle = el("theme-toggle");
      if (themeToggle) {
        themeToggle.textContent = state.settings.theme === "light" ? "Dark" : "Light";
        themeToggle.title = state.settings.theme === "light" ? "Switch to dark theme" : "Switch to light theme";
      }
      const gexModeToggle = el("gex-mode-toggle");
      if (gexModeToggle) {
        const gexActive = normalizeIndicatorMode(state.settings.indicatorMode) === "gex";
        gexModeToggle.classList.toggle("active", gexActive);
        gexModeToggle.setAttribute("aria-pressed", gexActive ? "true" : "false");
        gexModeToggle.title = gexActive
          ? `${state.symbol} GEX indicator preset is active`
          : `${state.symbol} regular indicator preset is active`;
      }
      const presetSelect = el("view-preset-select");
      if (presetSelect) {
        const preset = normalizeViewPreset(state.settings.viewPreset);
        presetSelect.value = ["alerts", "mobile"].includes(preset) ? preset : "trading";
      }
      el("health-visualizer-select").value = normalizeHealthVisualizer(state.settings.healthVisualizer);
      const motionSelect = el("motion-preference-select");
      if (motionSelect) motionSelect.value = motionPreference;
      el("horizontal-grid-toggle").checked = state.settings.showHorizontalGrid;
      el("vertical-grid-toggle").checked = state.settings.showVerticalGrid;
      el("candle-grid-toggle").checked = state.settings.showCandleGrid;
      el("bid-ask-candle-toggle").checked = state.settings.showBidAskOnCandle;
      el("economic-calendar-toggle").checked = Boolean(state.settings.economicCalendarEnabled);
      const priceSessionOpacity = clamp(Number(state.settings.sessionPriceOpacity) || 0, 0, 8);
      const volumeSessionOpacity = clamp(Number(state.settings.sessionVolumeOpacity) || 0, 0, 10);
      el("session-price-opacity").value = String(priceSessionOpacity);
      el("session-price-opacity").setAttribute("aria-valuetext", `${priceSessionOpacity}%`);
      el("session-price-opacity-label").textContent = `${priceSessionOpacity}%`;
      el("session-volume-opacity").value = String(volumeSessionOpacity);
      el("session-volume-opacity").setAttribute("aria-valuetext", `${volumeSessionOpacity}%`);
      el("session-volume-opacity-label").textContent = `${volumeSessionOpacity}%`;
      el("signals-range-select").value = normalizeSignalsRange(state.settings.signalsRange);
      el("signal-density-select").value = normalizeSignalDensity(state.settings.signalDensity);
      syncAdvisorChartModeClass();
      refreshAdvisorToggleButton();
      el("indicator-label-style-select").value = normalizeIndicatorLabelStyle(state.settings.indicatorLabelStyle);
      const uiShapeSelect = el("ui-shape-select");
      if (uiShapeSelect) uiShapeSelect.value = uiShape;
      const paperMinRr = [0.75, 1.25, 1.5, 2].includes(Number(state.settings.paperMinRr)) ? Number(state.settings.paperMinRr) : 1.25;
      state.settings.paperMinRr = paperMinRr;
      el("paper-min-rr-select").value = String(paperMinRr);
      const paperAutoTradingToggle = el("paper-auto-trading-toggle");
      const paperAutoTradingInstrumentId = exactIdentityText(state.instrumentId);
      const paperAutoTradingInstrument = instrumentForId();
      paperAutoTradingToggle.checked = Boolean(
        paperAutoTradingInstrumentId && paperAutoTradingInstrument && state.settings.paperAutoTrading,
      );
      paperAutoTradingToggle.disabled = !paperAutoTradingInstrumentId || !paperAutoTradingInstrument;
      paperAutoTradingToggle.parentElement.title = paperAutoTradingInstrumentId && paperAutoTradingInstrument
        ? `Automatic paper entries for ${paperAutoTradingInstrument.display || paperAutoTradingInstrument.key || paperAutoTradingInstrumentId}`
        : "Select a provider-qualified instrument before enabling automatic paper entries";
      el("paper-edge-gate-toggle").checked = Boolean(state.settings.paperEdgeGate);
      el("paper-telegram-feed-toggle").checked = Boolean(state.settings.paperTelegramFeed);
      el("gex-scheduler-toggle").checked = Boolean(state.settings.gexSchedulerEnabled);
      const selectedGexSchedulerIds = new Set(
        Array.isArray(state.settings.gexSchedulerInstrumentIds)
          ? state.settings.gexSchedulerInstrumentIds
          : []
      );
      for (const option of el("gex-scheduler-instrument-ids").options) {
        option.selected = selectedGexSchedulerIds.has(option.value);
      }
      const currentOptionInstrumentId = exactIdentityText(state.instrumentId);
      const currentOptionInstrument = instrumentForId();
      const currentOptionInstrumentLabel = currentOptionInstrument?.display || currentOptionInstrument?.key || state.symbol || "Current instrument";
      const currentOptionCap = Number(state.settings.optionCaps?.[currentOptionInstrumentId]);
      const optionCapInput = el("option-cap-current");
      const optionCapInstrument = el("option-cap-instrument");
      optionCapInput.value = Number.isFinite(currentOptionCap) && currentOptionCap > 0 ? String(currentOptionCap) : "";
      optionCapInput.disabled = !currentOptionInstrumentId;
      optionCapInput.title = currentOptionInstrumentId
        ? `Maximum option premium for ${currentOptionInstrumentLabel} (${currentOptionInstrumentId})`
        : "Select a provider-qualified instrument before setting an option premium cap";
      optionCapInstrument.textContent = currentOptionInstrumentLabel;
      optionCapInstrument.title = currentOptionInstrumentId;
      el("option-targets-toggle").checked = state.indicators.optionTargets.enabled;
      el("option-targets-fast-status").checked = state.indicators.optionTargets.fastStatus;
      el("option-targets-pulse").checked = state.indicators.optionTargets.pulse;
      renderOptionTargetActiveList();
      const ibkrPortInput = el("ibkr-port-input");
      if (ibkrPortInput) ibkrPortInput.value = String(clamp(Number(state.settings.ibkrPort) || 7497, 1, 65535));
      el("global-default-preset").value = ["balanced", "scalp", "conservative", "research"].includes(state.indicators.globalDefaults.preset) ? state.indicators.globalDefaults.preset : "balanced";
      el("global-atr-len").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.atrLen, 14), 2, 100));
      el("global-rvol-len").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolLen, 30), 2, 200));
      el("global-ema-pullback").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaPullback, 20), 5, 300));
      el("global-ema-fast").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaFast, 21), 5, 300));
      el("global-ema-slow").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaSlow, 55), 10, 400));
      el("global-ema-magnet").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaMagnet, 233), 50, 1000));
      el("global-score-pre").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scorePre, 42), 0, 99));
      el("global-score-watch").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scoreWatch, 58), 0, 99));
      el("global-score-arm").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scoreArm, 70), 0, 99));
      el("global-score-go").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scoreGo, 78), 0, 99));
      el("global-rvol-low").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolLow, 0.85), 0, 5));
      el("global-rvol-elevated").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolElevated, 1.10), 0, 5));
      el("global-rvol-high").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolHigh, 1.20), 0, 5));
      el("global-rvol-climax").value = String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolClimax, 1.60), 0, 10));
      el("ema-all-toggle").checked = state.indicators.ema233.allEnabled;
      el("ema20-toggle").checked = state.indicators.ema233.ema20Enabled;
      el("ema50-toggle").checked = state.indicators.ema233.ema50Enabled;
      el("ema20-color").value = state.indicators.ema233.ema20Color || css("--ema20") || "#facc15";
      el("ema50-color").value = state.indicators.ema233.ema50Color || (isLightTheme() ? "#7c2d12" : "#22d3ee");
      el("ema233-color").value = state.indicators.ema233.color || css("--ema233") || "#38bdf8";
      el("ema233-toggle").checked = state.indicators.ema233.enabled;
      el("ema233-style").value = state.indicators.ema233.style;
      el("ema233-width").value = String(clamp(Number(state.indicators.ema233.width) || 2, 1, 4));
      state.indicators.ema233.alertRearmMinutes = ema233AlertRearmSetting(state.indicators.ema233.alertRearmMinutes);
      el("ema233-alert-rearm").value = String(state.indicators.ema233.alertRearmMinutes);
      el("vwap-toggle").checked = state.indicators.vwap.enabled;
      el("vwap-style").value = state.indicators.vwap.style;
      el("vwap-width").value = String(clamp(Number(state.indicators.vwap.width) || 1, 1, 3));
      el("vwap-color").value = state.indicators.vwap.color || css("--vwap") || "#22c55e";
      el("vwap-bands-toggle").checked = state.indicators.vwap.bands;
      el("vwap-band-count").value = String(clamp(Number(state.indicators.vwap.bandCount) || 2, 1, 3));
      el("vwap-band-color").value = state.indicators.vwap.bandColor || css("--canvas-blue") || "#38bdf8";
      el("vsa-volume-toggle").checked = state.indicators.vsaVolume.visible !== false;
      el("vsa-volume-avg").checked = state.indicators.vsaVolume.avg !== false;
      el("vsa-volume-labels").checked = state.indicators.vsaVolume.labels !== false;
      el("vsa-volume-price-marks").checked = state.indicators.vsaVolume.priceMarks !== false;
      el("vsa-volume-candle-colors").checked = state.indicators.vsaVolume.candleColors !== false;
      el("vsa-volume-volume-colors").checked = state.indicators.vsaVolume.volumeColors === true;
      el("vsa-volume-render-hours").value = String(vsaVolumeRenderHours());
      syncManifestIndicatorControlInputs();
      syncIndicatorModuleSettings({ resetInvalid: true });
      el("strategy-mode-select").value = state.strategyMode || "balanced";
      renderSettingsGroupSummaries();
      el("gex-context-toggle").checked = state.indicators.gexContext.enabled;
      el("gex-dynamics-toggle").checked = Boolean(state.indicators.gexContext.dynamicsVisible);
      const gexDisplayLevels = Number(state.indicators.gexContext.displayLevels);
      el("gex-display-levels").value = String(
        GEX_DISPLAY_LEVEL_COUNTS.includes(gexDisplayLevels)
          ? gexDisplayLevels
          : GEX_DEFAULT_DISPLAY_LEVEL_COUNT
      );
      renderGexSidebarChrome();
      if (typeof renderOptionBoardButton === "function") renderOptionBoardButton();
      el("ny-range-toggle").checked = state.indicators.nyRange.enabled;
      el("ny-range-opacity").value = String(clamp(Number(state.indicators.nyRange.opacity) || 14, 8, 28));
      el("ny-range-style").value = state.indicators.nyRange.style;
      el("ny-range-lines").checked = state.indicators.nyRange.priceLines;
      el("prev-day-levels-toggle").checked = Boolean(state.indicators.nyRange.prevDayLevels);
      applyDrawingUi();
      if (typeof renderVsaVolumeColorLegend === "function") renderVsaVolumeColorLegend();
      renderSystemHealth();
      renderVitalityHealth();
      if (typeof syncAllManagedIndicatorChartToggles === "function") syncAllManagedIndicatorChartToggles();
      if (typeof updateIndicatorCalcBadges === "function") updateIndicatorCalcBadges();
      if (typeof renderIndicatorSettingsLinkBadges === "function") renderIndicatorSettingsLinkBadges();
      if (typeof refreshIndicatorManager === "function") refreshIndicatorManager();
      if (typeof bumpChartRenderVersion === "function") {
        bumpChartRenderVersion("settings");
        bumpChartRenderVersion("indicators");
      }
      if (state.snapshot) renderCharts(state.snapshot);
    }

    function showRuntimeError(error, phase = "runtime") {
      const message = error?.message || String(error || "Unknown UI error");
      const stackLine = String(error?.stack || "").split("\n").slice(1, 2).join("").trim();
      const detail = `${phase}: ${message}${stackLine ? ` (${stackLine})` : ""}`;
      console.error("MartinCall UI error", error);
      const title = document.getElementById("chart-title");
      const notice = document.getElementById("history-notice");
      if (title) title.textContent = "UI ERROR";
      if (notice) {
        notice.textContent = detail;
        notice.classList.add("show");
      }
    }

    window.addEventListener("error", event => showRuntimeError(event.error || event.message, "window.error"));
    window.addEventListener("unhandledrejection", event => showRuntimeError(event.reason, "promise"));

    const HEARTBEAT_TICK_MS = 1000;

    function heartbeatDue(key, now, everyMs) {
      if (!state.heartbeatSchedule) state.heartbeatSchedule = {};
      const last = Number(state.heartbeatSchedule[key] || 0);
      if (now - last < everyMs) return false;
      state.heartbeatSchedule[key] = now;
      return true;
    }

    function runHeartbeatTasks(now = Date.now()) {
      maintainBrowserCaptureConnection(now);
      const visible = document.visibilityState === "visible";
      const activeOwner = ownsBackgroundPolling();
      if (visible && heartbeatDue("presentation_freshness", now, 1000)) {
        if (typeof refreshWatchlistFreshness === "function") refreshWatchlistFreshness(now);
        if (typeof refreshPaperTradingFreshness === "function") refreshPaperTradingFreshness(now);
        if (typeof refreshOptionBoardFreshness === "function") refreshOptionBoardFreshness(now);
        if (typeof refreshOptionDriveFreshness === "function") refreshOptionDriveFreshness(now);
      }
      const sharedObjectsPollingActive = visible
        && activeOwner
        && storagePollingActive({ poll: true, activeOwner: true });
      const settingsSnapshotDue = (
        visible
        && !state.serverSleeping
        && activeOwner
        && serverStorageReady
        && heartbeatDue(
          "settings_snapshot",
          now,
          SERVER_SETTING_SNAPSHOT_RECONCILE_MS,
        )
      );
      const combinedStorageReconcile = Boolean(
        settingsSnapshotDue
        && sharedObjectsPollingActive
        && !localObjectEditActive()
      );
      if (settingsSnapshotDue) {
        const reconciliation = combinedStorageReconcile
          ? refreshSharedObjectsFromServer({
              poll: true,
              activeOwner: true,
              reconcileSettings: true,
            })
          : reconcileServerSettingsFromBackend();
        if (combinedStorageReconcile) {
          state.heartbeatSchedule.shared_objects = now;
        }
        reconciliation.catch(error => {
          console.warn(
            "server settings snapshot reconciliation failed",
            requestErrorMessage(error, "server settings snapshot reconciliation failed"),
          );
        });
      }
      if (
        visible
        && !state.serverSleeping
        && gexLayerVisible()
        && heartbeatDue("gex_age_label", now, GEX_CONTEXT_POLL_MS)
      ) {
        renderGexContextModeButton();
      }
      if (
        visible
        && !state.serverSleeping
        && activeOwner
        && !state.requests.history.loading
        && heartbeatDue("market", now, AUTO_REFRESH_MS)
      ) {
        const streamBacked = providerCapability(state.dataSource, "chart_stream");
        const streamFresh = streamBacked
          && typeof chartStreamFreshForRender === "function"
          && chartStreamFreshForRender(now);
        const refreshMs = streamBacked ? CHART_STREAM_MARKET_REFRESH_MS : AUTO_REFRESH_MS;
        if (!streamFresh && now - Number(state.lastMarketLoadAt || 0) >= refreshMs) {
          load({ queueAnalysis: false });
        }
      }
      if (
        visible
        && !state.serverSleeping
        && activeOwner
        && (
          providerCapability(state.dataSource, "live_quote_stream")
          || providerCapability(state.dataSource, "live_quote_polling")
        )
        && heartbeatDue("quote", now, QUOTE_REFRESH_MS)
        && now - Number(state.lastQuoteAt || 0) >= QUOTE_REFRESH_MS
      ) {
        loadLiveQuote();
      }
      if (visible && !state.serverSleeping && activeOwner && heartbeatDue("screener", now, quoteStreamOpen() ? SCREENER_STREAM_REFRESH_MS : SCREENER_REFRESH_MS)) {
        loadScreener();
      }
      if (
        visible
        && !state.serverSleeping
        && activeOwner
        && gexLayerVisible()
        && heartbeatDue("gex_context", now, GEX_CONTEXT_POLL_MS)
        && now - Number(state.lastGexContextPollAt || 0) >= GEX_CONTEXT_POLL_MS
      ) {
        state.lastGexContextPollAt = now;
        loadGexContext({ refresh: false, bypassCache: true });
      }
      if (
        visible
        && !state.serverSleeping
        && activeOwner
        && paperPollingActive({ poll: true, activeOwner: true })
        && heartbeatDue("paper_stats", now, 5000)
        && now - Number(state.lastPaperStatsLoadAt || 0) >= 10000
      ) {
        loadPaperStats({ poll: true, activeOwner: true });
        loadOptionDrivePaperStats({ poll: true });
      }
      if (
        visible
        && !state.serverSleeping
        && activeOwner
        && heartbeatDue("indicator_lifecycles", now, 1000)
        && typeof syncIndicatorModuleLifecycles === "function"
      ) {
        void syncIndicatorModuleLifecycles({
          snapshot: state.snapshot,
          poll: true,
          reason: "heartbeat",
        });
      }
      if (visible && heartbeatDue("passive_render", now, PASSIVE_RENDER_MS) && passiveChartRenderDue()) {
        ensureCurrentLiveCandle();
        state.lastPassiveChartRenderAt = now;
        renderCharts(state.snapshot);
      }
      if (visible && activeOwner && heartbeatDue("system_health", now, 30000)) {
        loadSystemHealth();
      }
      if (visible && heartbeatDue("vitality_health", now, 1000)) {
        renderVitalityHealth();
      }
      if (sharedObjectsPollingActive) {
        if (
          !combinedStorageReconcile
          && heartbeatDue("shared_objects", now, SHARED_OBJECT_SYNC_MS)
        ) {
          refreshSharedObjectsFromServer({ poll: true, activeOwner: true });
        }
      }
    }

    async function boot() {
      try {
        debugStep("boot started");
        document.getElementById("chart-title").textContent = "Loading MartinCall...";
        await loadProviders();
        await loadServerSettings();
        setupInstrumentList();
        renderInstruments();
        renderTimeframes();
        setupIndicatorQuickToggles();
        setupIndicatorRuntimeToggles();
        setupDirectionSentimentTooltip();
        syncTradeSetupRuntimeLayoutButtons();
        setupAppViewportHeight();
        applySettings();
        applyLayout();
        setupSplitters();
        setupChartInteractions();
        setupWindowLinkChannel();
        setupToolbar();
        setupDrawingTools();
        setupOptionBoard();
        setupOptionDrive();
        setupIndicatorLens();
        setupWorkspaceDockControls();
        setupGexDock();
        setupWatchlistControls();
        setupSideTabs();
        setupAlertManager();
        setupPaperTradingControls();
        setupSettings();
        await loadInstruments({ activate: false });
        await loadServerStorage();
        if (typeof syncIndicatorModuleLifecycles === "function") {
          await syncIndicatorModuleLifecycles({ force: true });
        }
        activateLoadedWatchlistRuntime();
        setupIndicatorQuickToggles();
        renderInstruments();
        renderTimeframes();
        applySettings();
        applyLayout();
        applySideTabs();
        syncWorkspacePageState();
        setupBrowserCapture();
        const initialMarketLoad = instrumentForId()
          ? load({ historyLoad: true, initialViewport: true })
          : Promise.resolve(null);
        Promise.allSettled([
          loadSystemHealth(),
        ]);
        void startEconomicCalendarRuntime();
        initialMarketLoad.finally(() => {
          if (typeof syncIndicatorModuleLifecycles === "function") {
            void syncIndicatorModuleLifecycles({
              snapshot: state.snapshot,
              reason: "initial-market-complete",
            });
          }
          debugStep("initial market complete", `${state.snapshot?.bars?.length || 0} bars`);
        });
        debugStep("boot complete", "market loading async");
        setInterval(() => runHeartbeatTasks(Date.now()), HEARTBEAT_TICK_MS);
        document.getElementById("history-notice")?.addEventListener("click", copyDataQualityStatus);
        document.getElementById("data-health-badge")?.addEventListener("click", copyDataQualityStatus);
        window.addEventListener("storage", event => {
          if (
            event.key === "aef:paperJournalUpdatedAt"
            && document.visibilityState === "visible"
            && paperPollingActive({ poll: true })
          ) {
            loadPaperStats({ poll: true });
            loadOptionDrivePaperStats({ poll: true });
          }
          applySharedObjectsFromLocalStorageEvent(event);
        });
        const reloadMarketAfterVisibilityResume = async (options = {}) => {
          if (document.visibilityState !== "visible") return;
          const now = Date.now();
          if (!options.force && now - Number(state.lastVisibilityReloadAt || 0) < 1500) return;
          state.lastVisibilityReloadAt = now;
          refreshSharedObjectsFromServer();
          try {
            await loadInstruments({ activate: false });
          } catch (error) {
            debugStep(
              "watchlist resume",
              requestErrorMessage(error, "watchlist reconciliation failed"),
            );
          }
          loadWatchlistTrends();
          connectQuoteStream(false);
          connectChartStream(false);
          if (currentOptionBoardState().open) connectOptionBoard();
          if (state.serverSleeping) return;
          loadSystemHealth({ force: true });
          syncEconomicCalendarRuntime({ refresh: true });
          if (gexLayerVisible()) {
            if (gexLiveModeRequested()) connectGexLiveStream();
            loadGexContext({ refresh: false, bypassCache: true, refreshPoll: true });
          }
          if (
            providerCapability(state.dataSource, "chart_stream")
            && chartStreamAllowedForRange()
          ) {
            flushQueuedChartBars();
            if (state.snapshot?.bars?.length) {
              renderLiveMarketVisuals(state.snapshot, { forcePanel: true });
            }
            connectChartStream(false);
            return;
          }
          if (state.requests.market.loading) return;
          load({ force: true, queueAnalysis: true, requireLatestAnalysis: true, visibilityResume: true });
        };
        window.addEventListener("focus", () => {
          reloadMarketAfterVisibilityResume();
        });
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState === "visible") {
            setupWindowLinkChannel();
            ensureOptionTargetPulseRender();
            reloadMarketAfterVisibilityResume();
          } else {
            state.lastVisibilityHiddenAt = Date.now();
            stopOptionTargetPulseRender();
            if (state.requests.market.abort) state.requests.market.abort.abort();
            abortMarketAnalysis();
            releaseMarketAnalysisLease({ beacon: true });
            suspendOptionBoard();
          }
        });
        window.addEventListener("pagehide", () => {
          flushServerSettings();
          stopEconomicCalendarRuntime();
          if (state.requests.market.abort) state.requests.market.abort.abort();
          abortMarketAnalysis();
          releaseMarketAnalysisLease({ beacon: true });
          closeOptionBoard({ reason: "page hidden" });
          void stopGexLiveContext({
            pruneRouteCaches: true,
            reason: "page hidden",
          });
          closeWindowLinkChannel();
          closeQuoteStream();
          closeChartStream();
        });
        window.addEventListener("pageshow", event => {
          setupWindowLinkChannel();
          if (event.persisted) void startEconomicCalendarRuntime();
          if (event.persisted) reloadMarketAfterVisibilityResume({ force: true });
        });
        window.addEventListener("resize", () => {
          scheduleAppViewportHeightSync();
        });
      } catch (error) {
        debugStep("boot error", requestErrorMessage(error, "boot failed"));
        showRuntimeError(error);
      }
    }

    boot();
