    function applyServerSettings(settings, options = {}) {
      if (!settings || typeof settings !== "object") return;
      const preserveNavigation = Boolean(options.preserveNavigation);
      const initialHydration = Boolean(options.initialHydration);
      const applyVisuals = Boolean(options.applyVisuals);
      const managedIndicatorEntries = typeof MANAGED_INDICATOR_UI !== "undefined"
        ? MANAGED_INDICATOR_UI
        : [];
      const previousIndicatorCalc = new Map(
        managedIndicatorEntries.map(entry => [
          entry.id || entry.group,
          state.indicators?.[entry.group]
            ? state.indicators[entry.group].enabled !== false
            : null,
        ]),
      );
      const previousGexLayerVisible = typeof gexLayerVisible === "function"
        ? gexLayerVisible()
        : false;
      const previousGexContextMode = String(
        state.indicators?.gexContext?.mode || "request",
      );
      const previousGexSidebarWidth = Number(layout.gexSidebarWidth);
      const previousDockLayout = [layout.workspaceDockWidth, layout.workspaceDockSide, layout.gexSidebarPlacement].join("|");
      const previousSideWidth = Number(layout.sideWidth);
      const previousVolumeHeight = Number(layout.volumeHeight);
      const previousEconomicCalendarEnabled = Boolean(
        state.settings.economicCalendarEnabled,
      );
      const currentInstrumentId = state.instrumentId;
      const currentSymbol = state.symbol;
      const currentTimeframe = state.timeframe;
      const currentRange = state.range;
      const currentSideTab = state.sideTab;
      for (const [key, value] of Object.entries(settings)) {
        if (isCurrentClientSettingKey(key) && typeof value === "string") {
          serverSettings[String(key)] = value;
        }
      }
      if (preserveNavigation) {
        state.instrumentId = currentInstrumentId;
        state.symbol = currentSymbol;
        state.timeframe = currentTimeframe;
        state.range = currentRange;
        state.sideTab = currentSideTab;
        workspaceSessionSet("instrumentId", currentInstrumentId);
        workspaceSessionSet("timeframe", currentTimeframe);
      } else {
        const nextInstrumentId = initialHydration
          ? exactIdentityText(
              initialInstrumentIdFromUrl !== null
                ? initialInstrumentIdFromUrl
                : workspaceServerValue("instrumentId") || "",
            )
          : workspaceValue("instrumentId") || state.instrumentId;
        const nextTimeframe = initialHydration
          ? String(workspaceServerValue("timeframe") || "5m")
          : workspaceValue("timeframe") || state.timeframe;
        if (nextInstrumentId !== state.instrumentId || nextTimeframe !== state.timeframe) {
          if (typeof mtfLensPrepareForPrimaryScopeChange === "function") {
            mtfLensPrepareForPrimaryScopeChange();
          }
          closeQuoteStream();
        }
        state.instrumentId = nextInstrumentId;
        const selected = instrumentForId();
        if (selected) state.symbol = String(selected.key || selected.display || "").trim();
        state.timeframe = nextTimeframe;
      }
      state.dataSource = String(instrumentForId()?.provider || "").trim().toLowerCase();
      if (typeof mtfLensRebindCurrentScope === "function") mtfLensRebindCurrentScope();
      state.sideTab = initialHydration
        ? "instruments"
        : preserveNavigation
          ? currentSideTab
          : workspaceValue("sideTab") || state.sideTab;
      state.range = preserveNavigation ? currentRange : storedRangeForInstrument(state.instrumentId, state.timeframe);
      const themeSetting = initialHydration
        ? workspaceServerValue("theme")
        : workspaceAuthoritativeValue("theme", state.settings.theme);
      state.settings.theme = normalizeThemeSetting(
        themeSetting,
        "dark",
      );
      workspaceSessionSet("theme", state.settings.theme);
      if (!initialHydration && workspaceSessionDirty("theme") && workspaceServerValue("theme") !== state.settings.theme) {
        setServerSettingValue(workspaceKey("theme"), state.settings.theme);
      } else if (workspaceServerValue("theme") === state.settings.theme) {
        workspaceSessionClearDirty("theme");
      }
      state.settings.showHorizontalGrid = storedBool("aef:showHorizontalGrid", state.settings.showHorizontalGrid);
      state.settings.showVerticalGrid = storedBool("aef:showVerticalGrid", state.settings.showVerticalGrid);
      state.settings.showCandleGrid = storedBool("aef:showCandleGrid", state.settings.showCandleGrid);
      state.settings.showBidAskOnCandle = storedBool("aef:showBidAskOnCandle", state.settings.showBidAskOnCandle);
      state.settings.economicCalendarEnabled = storedBool(
        "aef:economicCalendar",
        state.settings.economicCalendarEnabled,
      );
      if (
        previousEconomicCalendarEnabled !== state.settings.economicCalendarEnabled
        && state.economicCalendar?.started
        && typeof syncEconomicCalendarRuntime === "function"
      ) {
        void syncEconomicCalendarRuntime({
          forceRefresh: state.settings.economicCalendarEnabled,
        });
      }
      state.settings.healthVisualizer = normalizeHealthVisualizer(
        serverSettingValue("aef:healthVisualizer") || loadHealthVisualizerSetting(),
      );
      state.settings.motionPreference = normalizeMotionPreference(
        serverSettingValue("aef:motionPreference") || state.settings.motionPreference,
      );
      state.settings.sessionPriceOpacity = Number(serverSettingValue("aef:sessionPriceOpacity") ?? state.settings.sessionPriceOpacity);
      state.settings.sessionVolumeOpacity = Number(serverSettingValue("aef:sessionVolumeOpacity") ?? state.settings.sessionVolumeOpacity);
      state.settings.signalsRange = normalizeSignalsRange(serverSettingValue("aef:signalsRange") || state.settings.signalsRange);
      state.settings.signalDensity = normalizeSignalDensity(serverSettingValue("aef:signalDensity") || state.settings.signalDensity);
      state.settings.chartViewMode = normalizeChartViewMode(serverSettingValue("aef:chartViewMode") || state.settings.chartViewMode);
      state.settings.uiShape = normalizeUiShape(serverSettingValue("aef:uiShape") || state.settings.uiShape);
      state.settings.indicatorLabelStyle = normalizeIndicatorLabelStyle(serverSettingValue("aef:indicatorLabelStyle") || state.settings.indicatorLabelStyle);
      const gexFocusSetting = initialHydration
        ? workspaceServerValue("gexFocusMode")
        : workspaceAuthoritativeValue("gexFocusMode");
      state.settings.gexFocusMode = gexFocusSetting === null
        ? false
        : gexFocusSetting !== "false";
      const viewPresetSetting = initialHydration
        ? workspaceServerValue("viewPreset")
        : workspaceAuthoritativeValue("viewPreset", state.settings.viewPreset);
      state.settings.viewPreset = normalizeViewPreset(viewPresetSetting);
      state.settings.ibkrPort = Number(serverSettingValue("aef:ibkrPort") || state.settings.ibkrPort || 7497);
      const storedPaperMinRr = Number(serverSettingValue("aef:paperMinRr"));
      state.settings.paperMinRr = [0.75, 1.25, 1.5, 2].includes(storedPaperMinRr)
        ? storedPaperMinRr
        : state.settings.paperMinRr;
      state.settings.paperEdgeGate = storedBool("aef:paperEdgeGate", state.settings.paperEdgeGate);
      state.settings.paperTelegramFeed = storedBool("aef:paperTelegramFeed", false);
      state.settings.tradeSetupRuntimeLayout = serverSettingValue("aef:tradeSetupRuntimeLayout") === "compact"
        ? "compact"
        : "full";
      state.paperTrading.showTradesOnChart = undefined;
      paperShowTradesOnChart();
      state.paperTrading.entryLabelFrameOffset = undefined;
      paperEntryLabelFrameOffset();
      state.drawing.cursorMode = normalizeCursorMode(
        serverSettingValue("aef:cursorMode") || state.drawing.cursorMode,
      );
      state.drawing.magnet = storedBool("aef:drawingMagnet", state.drawing.magnet);
      state.drawing.hidden = storedBool("aef:drawingHidden", state.drawing.hidden);
      view.barsVisible = storedBarsVisibleForInstrument(state.instrumentId, state.timeframe);
      view.priceZoom = Number(serverSettingValue("aef:priceZoom") || view.priceZoom);
      view.priceShift = Number(serverSettingValue("aef:priceShift") || view.priceShift);
      view.rightGapManual = serverSettingValue("aef:rightGapManual") === "true";
      const storedRightGapBars = Number(serverSettingValue("aef:rightGapBars"));
      view.rightGapBars = view.rightGapManual && Number.isFinite(storedRightGapBars)
        ? storedRightGapBars
        : DEFAULT_RIGHT_GAP_BARS;
      view.followLatest = serverSettingValue("aef:followLatest") !== "false";
      layout.gexSidebarWidth = Number(
        serverSettingValue("aef:gexSidebarWidth") || layout.gexSidebarWidth,
      );
      layout.workspaceDockWidth = Number(serverSettingValue("aef:workspaceDockWidth") ?? 300);
      layout.workspaceDockSide = serverSettingValue("aef:workspaceDockSide") ?? "right";
      layout.gexSidebarPlacement = serverSettingValue("aef:gexSidebarPlacement") ?? "chart";
      layout.sideWidth = Number(serverSettingValue("aef:sideWidth") || layout.sideWidth);
      layout.volumeHeight = Number(serverSettingValue("aef:volumeHeight") || layout.volumeHeight);
      if (initialHydration) {
        const hydratedWorkspace = {
          instrumentId: state.instrumentId,
          timeframe: state.timeframe,
          sideTab: state.sideTab,
          theme: state.settings.theme,
          viewPreset: state.settings.viewPreset,
          gexFocusMode: state.settings.gexFocusMode ? "true" : "false",
        };
        for (const [key, value] of Object.entries(hydratedWorkspace)) {
          workspaceSessionSet(key, value);
          workspaceSessionClearDirty(key);
        }
      }
      loadIndicatorSettingsForCurrent();
      loadPaperTradingInstrumentSettings();
      renderPaperTradingPanel();
      if (applyVisuals) {
        const layoutDimensionsChanged = (
          Number(layout.gexSidebarWidth) !== previousGexSidebarWidth
          || [layout.workspaceDockWidth, layout.workspaceDockSide, layout.gexSidebarPlacement].join("|") !== previousDockLayout
          || Number(layout.sideWidth) !== previousSideWidth
          || Number(layout.volumeHeight) !== previousVolumeHeight
        );
        if (layoutDimensionsChanged && typeof applyLayout === "function") {
          applyLayout();
        }
        if (typeof applySettings === "function") applySettings();
        if (typeof applyDrawingUi === "function") applyDrawingUi();

        const nextGexLayerVisible = typeof gexLayerVisible === "function"
          ? gexLayerVisible()
          : false;
        const nextGexContextMode = String(
          state.indicators?.gexContext?.mode || "request",
        );
        const gexContextModeChanged = (
          previousGexContextMode !== nextGexContextMode
        );
        const gexLayerVisibilityChanged = (
          previousGexLayerVisible !== nextGexLayerVisible
        );
        let gexRuntimeTransition = null;
        if (
          gexContextModeChanged
          && typeof setGexContextMode === "function"
        ) {
          gexRuntimeTransition = setGexContextMode(nextGexContextMode, {
            apply: false,
            load: nextGexLayerVisible && !gexLayerVisibilityChanged,
            persist: false,
            reloadAnalysis: false,
          });
          if (
            gexLayerVisibilityChanged
            && typeof loadGexContext === "function"
          ) {
            gexRuntimeTransition = Promise.resolve(gexRuntimeTransition).then(
              () => loadGexContext({
                refresh: false,
                force: true,
                bypassCache: true,
              }),
            );
          }
        } else if (
          gexLayerVisibilityChanged
          && typeof loadGexContext === "function"
        ) {
          gexRuntimeTransition = loadGexContext({
            refresh: false,
            force: true,
            bypassCache: true,
          });
        }
        if (gexRuntimeTransition && typeof gexRuntimeTransition.catch === "function") {
          void gexRuntimeTransition.catch(error => {
            console.warn(
              "server settings GEX runtime reconciliation failed",
              requestErrorMessage(error, "GEX runtime reconciliation failed"),
            );
          });
        }

        const changedAnalysisIndicators = managedIndicatorEntries
          .filter(entry => !entry.uiOnly && state.indicators?.[entry.group])
          .filter(entry => (
            previousIndicatorCalc.get(entry.id || entry.group)
            !== (state.indicators[entry.group].enabled !== false)
          ));
        const gexModeRequiresAnalysisReload = Boolean(
          gexContextModeChanged
          && nextGexLayerVisible
        );
        const gexVisibilityRequiresAnalysisReload = Boolean(
          gexLayerVisibilityChanged
        );
        if (
          (
            changedAnalysisIndicators.length
            || gexModeRequiresAnalysisReload
            || gexVisibilityRequiresAnalysisReload
          )
          && state.snapshot
          && (
            typeof snapshotMatchesCurrentRoute !== "function"
            || snapshotMatchesCurrentRoute()
          )
          && typeof reloadCurrentAnalysisOnly === "function"
        ) {
          const changedIds = [
            ...changedAnalysisIndicators
              .map(entry => entry.id || entry.group),
            ...(gexModeRequiresAnalysisReload ? ["gex-context-mode"] : []),
            ...(
              gexVisibilityRequiresAnalysisReload
                ? ["gex-context-visibility"]
                : []
            ),
          ]
            .sort()
            .join(",");
          reloadCurrentAnalysisOnly("server-settings-analysis-reconcile", {
            renderImmediate: false,
            versionKey: [
              "server-settings-analysis",
              changedIds,
              Date.now(),
            ].join("|"),
          });
        }
      }
    }

    async function loadServerSettings() {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          const storage = await fetchServerStorage("", state.timeframe, {
            drawings: false,
            alerts: false,
            force: true,
            expectedRouteFingerprint: "",
            reason: "settings_boot",
          });
          serverStorageReady = true;
          hydrateServerSettingMutationSnapshot(
            storage.settings,
            storage.settings_mutations,
            storage.settings_mutation_changed_at_ms,
            storage.settings_revision,
          );
          applyServerSettings(serverSettings, { initialHydration: true });
          return storage;
        } catch (error) {
          serverStorageReady = false;
          const errorPayload = normalizeErrorPayload(error?.payload);
          const retryable = !errorPayload || errorPayload?.error?.retryable === true;
          if (attempt === 0 && retryable) {
            debugStep("server settings retry", requestErrorMessage(error, "server settings load failed"));
            await sleep(SERVER_SETTING_RETRY_MS);
            continue;
          }
          throw error;
        }
      }
      throw new Error("server settings load exhausted its bounded retry");
    }

    function reconcileServerSettingsSnapshot(storage) {
      const snapshot = hydrateServerSettingMutationSnapshot(
        storage.settings,
        storage.settings_mutations,
        storage.settings_mutation_changed_at_ms,
        storage.settings_revision,
        { reconcile: true },
      );
      if (!snapshot.stale) {
        publishServerSettingMutations(snapshot.mutations, {
          snapshot: true,
          settings: snapshot.settings,
          settingsMutationChangedAtMs: snapshot.settings_mutation_changed_at_ms,
          settingsRevision: snapshot.settings_revision,
        });
      }
      return snapshot.changed;
    }

    function reconcileServerSettingsFromBackend() {
      if (!serverStorageReady) return Promise.resolve(false);
      if (serverSettingSnapshotReconcilePromise) return serverSettingSnapshotReconcilePromise;
      const requestTimeframe = String(state.timeframe || "");
      if (!requestTimeframe) {
        return Promise.reject(new TypeError("SERVER_STORAGE_SCOPE_INVALID: interval is required"));
      }
      const request = fetchServerStorage("", requestTimeframe, {
        drawings: false,
        alerts: false,
        force: true,
        expectedRouteFingerprint: "",
        reason: "settings_reconcile",
      })
        .then(storage => reconcileServerSettingsSnapshot(storage))
        .finally(() => {
          if (serverSettingSnapshotReconcilePromise === request) {
            serverSettingSnapshotReconcilePromise = null;
          }
        });
      serverSettingSnapshotReconcilePromise = request;
      return request;
    }
