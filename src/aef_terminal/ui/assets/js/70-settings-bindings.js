    function setupSettings() {
      const settingsPanel = document.getElementById("settings-panel");
      const settingsToggle = document.getElementById("settings-toggle");
      let settingsReturnFocus = settingsToggle;
      const closeSettings = ({ restoreFocus = true } = {}) => {
        if (!state.settings.open) return;
        state.settings.open = false;
        applySettings();
        if (restoreFocus) settingsReturnFocus?.focus?.();
      };
      setupFloatingPanelDrag("settings-panel", {
        root: document.getElementById("settings-panel"),
        surface: document.getElementById("settings-panel"),
        handle: document.querySelector("#settings-panel .settings-head"),
      });
      async function saveOptionCapSettings() {
        const instrumentId = exactIdentityText(state.instrumentId);
        const rawCap = Number(document.getElementById("option-cap-current").value);
        if (!instrumentId || !Number.isFinite(rawCap) || rawCap <= 0) return;
        const caps = { ...(state.settings.optionCaps || {}), [instrumentId]: clamp(rawCap, 0.01, 999) };
        state.settings.optionCaps = caps;
        try {
          const payload = await fetchJson("/api/options/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ premium_caps: caps }),
          });
          if (payload?.premium_caps) state.settings.optionCaps = payload.premium_caps;
        } catch (error) {
          console.warn("option caps save failed", requestErrorMessage(error, "option caps save failed"));
        }
        applySettings();
      }

      async function applyGexViewButtonMode(mode) {
        const next = ["off", "mini", "ladder", "expiry"].includes(mode) ? mode : "mini";
        if (next === "off") {
          state.indicators.gexContext.profile = false;
          saveInstrumentIndicatorSetting("gexContextProfile", "false");
        } else {
          state.indicators.gexContext.profile = true;
          state.indicators.gexContext.profileStyle = next;
          saveInstrumentIndicatorSetting("gexContextProfile", "true");
          saveInstrumentIndicatorSetting("gexContextProfileStyle", next);
          await loadGexContext({ refresh: false, force: true });
        }
        applySettings();
        syncWorkspacePageState();
        if (state.snapshot) renderCharts(state.snapshot);
      }

      async function applyGexZoneButtonMode(mode) {
        const next = ["off", "zones", "lines", "strike"].includes(mode) ? mode : "zones";
        state.indicators.gexContext.zoneStyle = next;
        state.indicators.gexContext.zones = next === "zones";
        saveInstrumentIndicatorSetting("gexContextZoneStyle", next);
        saveInstrumentIndicatorSetting("gexContextZones", state.indicators.gexContext.zones ? "true" : "false");
        if (state.indicators.gexContext.enabled && (next === "strike" || next === "zones" || next === "lines")) {
          await loadGexContext({ refresh: false, force: true });
        }
        applySettings();
        syncWorkspacePageState();
        if (state.snapshot) renderCharts(state.snapshot);
      }

      function applyGexHistoryView(value) {
        const next = gexHistoryView(value);
        state.indicators.gexContext.historyView = next;
        saveGexHistoryView(next);
        applySettings();
        syncWorkspacePageState();
        if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
      }

      function readManifestControlValue(control, node) {
        if (control.control_type === "toggle") return Boolean(node.checked);
        if (control.control_type === "number") {
          const fallback = Number(control.default);
          const minimum = Number(control.minimum);
          const maximum = Number(control.maximum);
          const low = Number.isFinite(minimum) ? minimum : -Infinity;
          const high = Number.isFinite(maximum) ? maximum : Infinity;
          const value = clamp(Number(node.value) || fallback, low, high);
          node.value = String(value);
          return value;
        }
        return node.value;
      }

      function saveManifestControlValue(control, value) {
        if (control.scope === "global") {
          saveGlobalIndicatorSetting(control.storage_key, typeof value === "boolean" ? (value ? "true" : "false") : String(value));
        } else if (control.scope === "instrument") {
          saveInstrumentIndicatorSetting(control.storage_key, typeof value === "boolean" ? (value ? "true" : "false") : String(value));
        } else {
          throw new Error(`INDICATOR_CONTROL_SCOPE_INVALID scope=${String(control.scope)}`);
        }
      }

      function applyManifestControlAction(action) {
        if (action === "load" || action === "load_apply") load(action === "load_apply" ? { force: true } : undefined);
        if (action === "apply" || action === "load_apply") applySettings();
        if (action === "render") {
          if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
          applySettings();
        }
      }

      function applyIndicatorProcessSideEffect(entry, group) {
        const handler = indicatorProcessEffectRegistry.get(String(entry.processEffectRef || ""));
        return typeof handler === "function"
          ? Boolean(handler({ entry, group }))
          : false;
      }

      function reloadAnalysisForIndicatorToggle(entry, reason = "settings") {
        const latest = typeof latestConfirmedIndicatorBar === "function"
          ? latestConfirmedIndicatorBar(state.snapshot)
          : null;
        const latestKey = timestampKey(latest?.ts || state.snapshot?.meta?.analysis_ts || "");
        const versionKey = [
          entry.id || entry.group,
          reason,
          state.indicators?.[entry.group]?.enabled !== false ? "calc1" : "calc0",
          state.indicators?.[entry.group]?.visible !== false ? "vis1" : "vis0",
          latestKey,
          Date.now(),
        ].join("|");
        reloadCurrentAnalysisOnly(`${entry.id || entry.group}:${reason}`, { versionKey });
      }

      async function applyManifestControlSideEffect(spec, control, value, group, node) {
        const effectRef = String(control?.effect_ref || "");
        if (effectRef === "table_position_state") {
          applyManifestDerivedStateRef(effectRef, group, value);
          return false;
        }
        const extensionHandler = indicatorControlEffectRegistry.get(effectRef);
        if (typeof extensionHandler === "function") {
          return Boolean(await extensionHandler({
            spec,
            control,
            value,
            group,
            node,
            saveControlValue: saveManifestControlValue,
          }));
        }
        return false;
      }

      function bindManifestIndicatorControls(indicatorIds = null) {
        const ids = Array.isArray(indicatorIds) ? new Set(indicatorIds) : null;
        Object.entries(indicatorRegistry()).forEach(([indicatorId, spec]) => {
          if (ids && !ids.has(indicatorId)) return;
          const ui = spec?.ui || {};
          const group = state.indicators?.[ui.state_key];
          if (!group) return;
          (spec.controls || []).forEach(control => {
            const node = document.getElementById(control.element_id);
            if (!node || node.dataset.manifestBindingReady === "1") return;
            node.dataset.manifestBindingReady = "1";
            const eventName = control.control_type === "color" ? "input" : "change";
            node.addEventListener(eventName, async () => {
              const value = readManifestControlValue(control, node);
              group[control.state_key || control.key] = value;
              saveManifestControlValue(control, value);
              if (await applyManifestControlSideEffect(spec, control, value, group, node)) return;
              applyManifestControlAction(control.action || "apply");
            });
          });
        });
      }

      settingsToggle.addEventListener("click", () => {
        if (!state.settings.open) settingsReturnFocus = document.activeElement || settingsToggle;
        state.settings.open = !state.settings.open;
        applySettings();
        if (state.settings.open) {
          requestAnimationFrame(() => clampFloatingPanelToViewport("settings-panel"));
          requestAnimationFrame(() => document.getElementById("settings-close")?.focus());
        } else {
          settingsReturnFocus?.focus?.();
        }
      });
      document.getElementById("system-diagnostics-request")?.addEventListener("click", async event => {
        const button = event.currentTarget;
        const status = document.getElementById("system-diagnostics-status");
        button.disabled = true;
        button.textContent = "Loading…";
        if (status) status.textContent = "Querying detailed PostgreSQL diagnostics…";
        try {
          await loadSystemHealth({ force: true, details: true });
          if (state.systemHealth?.storage?.ok !== true && status) {
            status.textContent = requestErrorMessage(
              state.systemHealth?.storage?.message,
              "Database diagnostics unavailable",
            );
          }
        } catch (error) {
          if (status) {
            status.textContent = requestErrorMessage(error, "Database diagnostics unavailable");
          }
        } finally {
          button.disabled = false;
          button.textContent = "Request DB diagnostics";
        }
      });
      document.getElementById("settings-close").addEventListener("click", () => {
        closeSettings();
      });
      document.addEventListener("pointerdown", event => {
        if (!state.settings.open) return;
        const target = event.target;
        if (settingsPanel?.contains(target) || settingsToggle?.contains(target)) return;
        closeSettings({ restoreFocus: false });
      });
      document.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !state.settings.open) return;
        event.preventDefault();
        closeSettings();
      });
      document.addEventListener("pointerdown", event => {
        if (!gexSidebarSettingsOpen()) return;
        const panel = document.getElementById("gex-sidebar-settings");
        const toggle = document.getElementById("gex-sidebar-settings-toggle");
        const target = event.target;
        if (panel?.contains(target) || toggle?.contains(target)) return;
        renderGexSidebarSettings(false);
      });
      document.getElementById("paper-journal-open")?.addEventListener("click", openPaperJournalWindow);
      document.getElementById("paper-journal-csv")?.addEventListener("click", downloadPaperJournalCsv);
      document.addEventListener("click", event => {
        const button = event.target.closest("#server-sleep-toggle, #backend-restart, #tws-reconnect, #ibkr-gateway-login, #telegram-alert-test-health");
        if (!button) return;
        if (button.id === "server-sleep-toggle") setServerSleepMode(!state.serverSleeping);
        else if (button.id === "backend-restart") restartBackendContainer();
        else if (button.id === "tws-reconnect") reconnectTwsConnection();
        else if (button.id === "ibkr-gateway-login") requestNewIbkrGatewayLogin();
        else if (button.id === "telegram-alert-test-health") sendTelegramTestAlert();
      });
      document.addEventListener("change", event => {
        if (event.target?.id === "telegram-interactive-toggle") {
          setTelegramInteractiveEnabled(event.target.checked);
          return;
        }
        if (event.target?.id !== "ibkr-port-input") return;
        const port = clamp(Math.round(Number(event.target.value) || 7497), 1, 65535);
        state.settings.ibkrPort = port;
        event.target.value = String(port);
        setServerSettingValue("aef:ibkrPort", String(port));
        const statusNode = document.getElementById("tws-reconnect-status");
        if (statusNode) statusNode.textContent = `IBKR port ${port}; reconnecting`;
        closeQuoteStream();
        closeChartStream();
        reconnectTwsConnection();
        applySettings();
      });
      document.getElementById("theme-toggle").addEventListener("click", () => {
        state.settings.theme = state.settings.theme === "light" ? "dark" : "light";
        workspaceSet("theme", state.settings.theme);
        applySettings();
        if (state.snapshot) renderCharts(state.snapshot);
      });
      document.getElementById("gex-mode-toggle")?.addEventListener("click", () => {
        applyIndicatorModeSelection(state.settings.gexMode ? "regular" : "gex");
      });
      document.getElementById("gex-context-mode-toggle")?.addEventListener("click", async event => {
        event.stopPropagation();
        await setGexContextMode(gexLiveModeRequested() ? "request" : "live");
      });
      document.getElementById("gex-sidebar-refresh")?.addEventListener("click", async event => {
        event.stopPropagation();
        if (event.currentTarget.disabled) return;
        await loadGexContext({ refresh: true });
      });
      document.getElementById("gex-sidebar-settings-toggle")?.addEventListener("click", event => {
        event.stopPropagation();
        renderGexSidebarSettings(!gexSidebarSettingsOpen());
      });
      document.getElementById("gex-sidebar-status")?.addEventListener("pointerenter", event => {
        const gex = activeGexContext(state.snapshot)
          || gexStatusContext(state.snapshot)
          || state.gexContext
          || null;
        event.currentTarget.title = gexSidebarStatusPresentation(
          gex,
          { includeDiagnostics: true },
        ).title;
      });
      document.getElementById("gex-sidebar-status")?.addEventListener("pointerleave", event => {
        const gex = activeGexContext(state.snapshot)
          || gexStatusContext(state.snapshot)
          || state.gexContext
          || null;
        event.currentTarget.title = gexSidebarStatusPresentation(gex).title;
      });
      document.getElementById("view-preset-select").addEventListener("change", event => {
        applyViewPresetSelection(event.target.value);
      });
      document.getElementById("gex-view-menu")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-gex-view-mode]");
        if (!button) return;
        event.stopPropagation();
        applyGexViewButtonMode(button.dataset.gexViewMode);
      });
      document.getElementById("gex-zone-menu")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-gex-zone-mode]");
        if (!button) return;
        event.stopPropagation();
        applyGexZoneButtonMode(button.dataset.gexZoneMode);
      });
      document.getElementById("gex-history-menu")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-gex-history-view]");
        if (!button) return;
        event.stopPropagation();
        applyGexHistoryView(button.dataset.gexHistoryView);
      });
      document.addEventListener("keydown", event => {
        if (event.key === "Escape" && gexSidebarSettingsOpen()) {
          renderGexSidebarSettings(false);
        }
      });
      document.getElementById("health-visualizer-select")?.addEventListener("change", event => {
        state.settings.healthVisualizer = normalizeHealthVisualizer(event.target.value);
        setServerSettingValue("aef:healthVisualizer", state.settings.healthVisualizer);
        applySettings();
        renderVitalityHealth();
      });
      document.getElementById("motion-preference-select")?.addEventListener("change", event => {
        state.settings.motionPreference = normalizeMotionPreference(event.target.value);
        setServerSettingValue("aef:motionPreference", state.settings.motionPreference);
        applySettings();
        renderVitalityHealth();
        if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
      });
      for (const [id, key, storageKey] of [
        ["horizontal-grid-toggle", "showHorizontalGrid", "aef:showHorizontalGrid"],
        ["vertical-grid-toggle", "showVerticalGrid", "aef:showVerticalGrid"],
        ["candle-grid-toggle", "showCandleGrid", "aef:showCandleGrid"],
        ["bid-ask-candle-toggle", "showBidAskOnCandle", "aef:showBidAskOnCandle"],
      ]) {
        document.getElementById(id).addEventListener("change", event => {
          state.settings[key] = event.target.checked;
          setServerSettingValue(storageKey, state.settings[key] ? "true" : "false");
          applySettings();
          if (state.snapshot) renderCharts(state.snapshot);
        });
      }
      document.getElementById("economic-calendar-toggle").addEventListener("change", event => {
        state.settings.economicCalendarEnabled = Boolean(event.target.checked);
        setServerSettingValue(
          "aef:economicCalendar",
          state.settings.economicCalendarEnabled ? "true" : "false",
        );
        applySettings();
        syncEconomicCalendarRuntime({ forceRefresh: state.settings.economicCalendarEnabled });
      });
      document.getElementById("session-price-opacity").addEventListener("input", event => {
        state.settings.sessionPriceOpacity = clamp(Number(event.target.value) || 0, 0, 8);
        setServerSettingValue("aef:sessionPriceOpacity", String(state.settings.sessionPriceOpacity));
        applySettings();
      });
      document.getElementById("session-volume-opacity").addEventListener("input", event => {
        state.settings.sessionVolumeOpacity = clamp(Number(event.target.value) || 0, 0, 10);
        setServerSettingValue("aef:sessionVolumeOpacity", String(state.settings.sessionVolumeOpacity));
        applySettings();
      });
      document.getElementById("signals-range-select").addEventListener("change", event => {
        state.settings.signalsRange = normalizeSignalsRange(event.target.value);
        setServerSettingValue("aef:signalsRange", state.settings.signalsRange);
        reloadCurrentAnalysisOnly("signals-range");
        applySettings();
      });
      document.getElementById("signal-density-select").addEventListener("change", event => {
        state.settings.signalDensity = normalizeSignalDensity(event.target.value);
        setServerSettingValue("aef:signalDensity", state.settings.signalDensity);
        if (state.snapshot) renderCharts(state.snapshot);
        applySettings();
      });
      document.getElementById("chart-advisor-toggle")?.addEventListener("click", () => {
        setChartViewMode(isAdvisorChartMode() ? "classic" : "advisor");
      });
      document.getElementById("indicator-label-style-select").addEventListener("change", event => {
        state.settings.indicatorLabelStyle = normalizeIndicatorLabelStyle(event.target.value);
        setServerSettingValue("aef:indicatorLabelStyle", state.settings.indicatorLabelStyle);
        if (state.snapshot) renderCharts(state.snapshot);
        applySettings();
      });
      document.getElementById("ui-shape-select")?.addEventListener("change", event => {
        state.settings.uiShape = normalizeUiShape(event.target.value);
        setServerSettingValue("aef:uiShape", state.settings.uiShape);
        applySettings();
      });
      document.getElementById("paper-min-rr-select").addEventListener("change", event => {
        const value = Number(event.target.value);
        state.settings.paperMinRr = [0.75, 1.25, 1.5, 2].includes(value) ? value : 1.25;
        setServerSettingValue("aef:paperMinRr", String(state.settings.paperMinRr));
        applySettings();
      });
      document.getElementById("paper-auto-trading-toggle").addEventListener("change", event => {
        const key = paperTradingInstrumentSettingKey("paperAutoTrading");
        const requested = Boolean(key && event.target.checked);
        const accepted = Boolean(
          key && setServerSettingValue(key, requested ? "true" : "false"),
        );
        state.settings.paperAutoTrading = accepted
          ? requested
          : Boolean(key && storedBool(key, false));
        event.target.checked = state.settings.paperAutoTrading;
        applySettings();
      });
      document.getElementById("paper-edge-gate-toggle").addEventListener("change", event => {
        state.settings.paperEdgeGate = Boolean(event.target.checked);
        setServerSettingValue("aef:paperEdgeGate", state.settings.paperEdgeGate ? "true" : "false");
        applySettings();
      });
      document.getElementById("paper-telegram-feed-toggle").addEventListener("change", event => {
        state.settings.paperTelegramFeed = Boolean(event.target.checked);
        setServerSettingValue("aef:paperTelegramFeed", state.settings.paperTelegramFeed ? "true" : "false");
        applySettings();
      });
      setupGexSettingsBindings();
      document.getElementById("option-cap-current").addEventListener("change", saveOptionCapSettings);
      document.getElementById("option-targets-toggle").addEventListener("change", event => {
        state.indicators.optionTargets.enabled = event.target.checked;
        saveInstrumentIndicatorSetting("optionTargetsEnabled", state.indicators.optionTargets.enabled ? "true" : "false");
        ensureOptionTargetPulseRender();
        applySettings();
      });
      document.getElementById("option-targets-fast-status").addEventListener("change", event => {
        state.indicators.optionTargets.fastStatus = event.target.checked;
        saveInstrumentIndicatorSetting("optionTargetsFastStatus", state.indicators.optionTargets.fastStatus ? "true" : "false");
        applySettings();
      });
      document.getElementById("option-targets-pulse").addEventListener("change", event => {
        state.indicators.optionTargets.pulse = event.target.checked;
        saveInstrumentIndicatorSetting("optionTargetsPulse", state.indicators.optionTargets.pulse ? "true" : "false");
        ensureOptionTargetPulseRender();
        applySettings();
      });
      function bindIndicatorProcessToggle(id, entry) {
        const input = document.getElementById(id);
        const group = state.indicators[entry.group];
        if (!input || !group) return;
        input.addEventListener("change", event => {
          group.enabled = event.target.checked;
          saveIndicatorSettingBool(entry.calcKey, group.enabled, Boolean(entry.global));
          if (entry.visibleKey) {
            if (!group.enabled) {
              group.visible = false;
              saveIndicatorSettingBool(entry.visibleKey, false, Boolean(entry.global));
            } else {
              group.visible = readIndicatorSettingBool(
                entry.visibleKey,
                entry.defaultVisible,
                Boolean(entry.global),
              );
            }
            coerceIndicatorVisibility(group);
          }
          syncManagedIndicatorChartToggle(entry);
          if (typeof updateIndicatorCalcBadges === "function") updateIndicatorCalcBadges();
          if (typeof renderIndicatorSettingsLinkBadges === "function") renderIndicatorSettingsLinkBadges();
          if (typeof refreshIndicatorManager === "function") refreshIndicatorManager();
          if (typeof renderAiThirdOpinionDiscussion === "function") renderAiThirdOpinionDiscussion(state.snapshot);
          if (applyIndicatorProcessSideEffect(entry, group)) {
            applySettings();
          } else if (entry.uiOnly) {
            if (state.snapshot) renderCharts(state.snapshot);
            applySettings();
          } else if (entry.apiVisibleKey) {
            applySettings();
            reloadAnalysisForIndicatorToggle(entry, "calc");
          } else {
            load();
            applySettings();
          }
        });
      }
      function bindIndicatorVisibleToggle(id, entry) {
        const input = document.getElementById(id);
        const group = state.indicators[entry.group];
        if (!input || !group) return;
        input.addEventListener("change", event => {
          const requestedVisible = event.target.checked;
          if (group.enabled === false) {
            event.target.checked = false;
            group.visible = false;
            return;
          }
          group.visible = requestedVisible;
          event.target.checked = requestedVisible;
          saveIndicatorSettingBool(entry.visibleKey, group.visible, Boolean(entry.global));
          coerceIndicatorVisibility(group);
          syncManagedIndicatorChartToggle(entry);
          applySettings();
          if (entry.apiVisibleKey) reloadAnalysisForIndicatorToggle(entry, "visible");
        });
      }
      MANAGED_INDICATOR_UI.forEach(entry => {
        bindIndicatorProcessToggle(entry.calcId, entry);
        bindIndicatorVisibleToggle(entry.chartId, entry);
      });
      function bindGlobalDefaultNumber(id, key, storageKey, fallback, low, high, stepReload = true) {
        const input = document.getElementById(id);
        input.addEventListener("change", event => {
          const value = clamp(numericValueOrFallback(event.target.value, fallback), low, high);
          state.indicators.globalDefaults[key] = value;
          event.target.value = String(value);
          saveGlobalIndicatorSetting(storageKey, String(value));
          applySettings();
          if (stepReload) reloadCurrentAnalysisOnly(`global-default:${key}`);
          else if (state.snapshot) renderCharts(state.snapshot);
        });
      }
      function saveGlobalDefaultValue(key, storageKey, value) {
        state.indicators.globalDefaults[key] = value;
        saveGlobalIndicatorSetting(storageKey, String(value));
      }
      function applyGlobalDefaultPreset(preset) {
        const next = ["balanced", "scalp", "conservative", "research"].includes(preset) ? preset : "balanced";
        const values = {
          balanced: {
            scoreWatch: 58, scoreArm: 70, scoreGo: 78, rvolLow: 0.85, rvolElevated: 1.10,
          },
          scalp: {
            scoreWatch: 56, scoreArm: 68, scoreGo: 76, rvolLow: 0.85, rvolElevated: 1.10,
          },
          conservative: {
            scoreWatch: 62, scoreArm: 74, scoreGo: 82, rvolLow: 0.90, rvolElevated: 1.15,
          },
          research: {
            scoreWatch: 50, scoreArm: 64, scoreGo: 72, rvolLow: 0.85, rvolElevated: 1.10,
          },
        }[next];
        state.indicators.globalDefaults.preset = next;
        saveGlobalIndicatorSetting("globalDefaultPreset", next);
        saveGlobalDefaultValue("scoreWatch", "globalScoreWatch", values.scoreWatch);
        saveGlobalDefaultValue("scoreArm", "globalScoreArm", values.scoreArm);
        saveGlobalDefaultValue("scoreGo", "globalScoreGo", values.scoreGo);
        saveGlobalDefaultValue("rvolLow", "globalRvolLow", values.rvolLow);
        saveGlobalDefaultValue("rvolElevated", "globalRvolElevated", values.rvolElevated);
        applySettings();
        reloadCurrentAnalysisOnly("global-default:preset");
      }
      document.getElementById("global-default-preset").addEventListener("change", event => {
        applyGlobalDefaultPreset(event.target.value);
      });
      bindGlobalDefaultNumber("global-atr-len", "atrLen", "globalAtrLen", 14, 2, 100);
      bindGlobalDefaultNumber("global-rvol-len", "rvolLen", "globalRvolLen", 30, 2, 200);
      bindGlobalDefaultNumber("global-ema-pullback", "emaPullback", "globalEmaPullback", 20, 5, 300);
      bindGlobalDefaultNumber("global-ema-fast", "emaFast", "globalEmaFast", 21, 5, 300);
      bindGlobalDefaultNumber("global-ema-slow", "emaSlow", "globalEmaSlow", 55, 10, 400);
      bindGlobalDefaultNumber("global-ema-magnet", "emaMagnet", "globalEmaMagnet", 233, 50, 1000);
      bindGlobalDefaultNumber("global-score-pre", "scorePre", "globalScorePre", 42, 0, 99);
      bindGlobalDefaultNumber("global-score-watch", "scoreWatch", "globalScoreWatch", 58, 0, 99);
      bindGlobalDefaultNumber("global-score-arm", "scoreArm", "globalScoreArm", 70, 0, 99);
      bindGlobalDefaultNumber("global-score-go", "scoreGo", "globalScoreGo", 78, 0, 99);
      bindGlobalDefaultNumber("global-rvol-low", "rvolLow", "globalRvolLow", 0.85, 0, 5);
      bindGlobalDefaultNumber("global-rvol-elevated", "rvolElevated", "globalRvolElevated", 1.10, 0, 5);
      bindGlobalDefaultNumber("global-rvol-high", "rvolHigh", "globalRvolHigh", 1.20, 0, 5);
      bindGlobalDefaultNumber("global-rvol-climax", "rvolClimax", "globalRvolClimax", 1.60, 0, 10);
      document.getElementById("ema-all-toggle").addEventListener("change", event => {
        state.indicators.ema233.allEnabled = event.target.checked;
        saveGlobalIndicatorSetting("emaAllEnabled", state.indicators.ema233.allEnabled ? "true" : "false");
        applySettings();
      });
      document.getElementById("ema20-toggle").addEventListener("change", event => {
        state.indicators.ema233.ema20Enabled = event.target.checked;
        saveGlobalIndicatorSetting("ema20Enabled", state.indicators.ema233.ema20Enabled ? "true" : "false");
        applySettings();
      });
      document.getElementById("ema50-toggle").addEventListener("change", event => {
        state.indicators.ema233.ema50Enabled = event.target.checked;
        saveGlobalIndicatorSetting("ema50Enabled", state.indicators.ema233.ema50Enabled ? "true" : "false");
        applySettings();
      });
      document.getElementById("ema20-color").addEventListener("input", event => {
        state.indicators.ema233.ema20Color = event.target.value;
        saveGlobalIndicatorSetting("ema20Color", state.indicators.ema233.ema20Color);
        applySettings();
      });
      document.getElementById("ema50-color").addEventListener("input", event => {
        state.indicators.ema233.ema50Color = event.target.value;
        saveGlobalIndicatorSetting("ema50Color", state.indicators.ema233.ema50Color);
        applySettings();
      });
      document.getElementById("ema233-color").addEventListener("input", event => {
        state.indicators.ema233.color = event.target.value;
        saveGlobalIndicatorSetting("ema233Color", state.indicators.ema233.color);
        applySettings();
      });
      document.getElementById("ema233-toggle").addEventListener("change", event => {
        state.indicators.ema233.enabled = event.target.checked;
        saveGlobalIndicatorSetting("ema233Enabled", state.indicators.ema233.enabled ? "true" : "false");
        applySettings();
      });
      document.getElementById("ema233-style").addEventListener("change", event => {
        state.indicators.ema233.style = event.target.value;
        saveGlobalIndicatorSetting("ema233Style", state.indicators.ema233.style);
        applySettings();
      });
      document.getElementById("ema233-width").addEventListener("change", event => {
        state.indicators.ema233.width = Number(event.target.value);
        saveGlobalIndicatorSetting("ema233Width", String(state.indicators.ema233.width));
        applySettings();
      });
      document.getElementById("ema233-alert-rearm").addEventListener("change", event => {
        state.indicators.ema233.alertRearmMinutes = ema233AlertRearmSetting(event.target.value);
        saveGlobalIndicatorSetting("ema233AlertRearmMinutes", String(state.indicators.ema233.alertRearmMinutes));
        applySettings();
      });
      document.getElementById("vwap-toggle").addEventListener("change", event => {
        state.indicators.vwap.enabled = event.target.checked;
        saveGlobalIndicatorSetting("vwapEnabled", state.indicators.vwap.enabled ? "true" : "false");
        applySettings();
      });
      document.getElementById("vwap-style").addEventListener("change", event => {
        state.indicators.vwap.style = event.target.value;
        saveGlobalIndicatorSetting("vwapStyle", state.indicators.vwap.style);
        applySettings();
      });
      document.getElementById("vwap-width").addEventListener("change", event => {
        state.indicators.vwap.width = Number(event.target.value);
        saveGlobalIndicatorSetting("vwapWidth", String(state.indicators.vwap.width));
        applySettings();
      });
      document.getElementById("vwap-color").addEventListener("input", event => {
        state.indicators.vwap.color = event.target.value;
        saveGlobalIndicatorSetting("vwapColor", state.indicators.vwap.color);
        applySettings();
      });
      document.getElementById("vwap-bands-toggle").addEventListener("change", event => {
        state.indicators.vwap.bands = event.target.checked;
        saveGlobalIndicatorSetting("vwapBands", state.indicators.vwap.bands ? "true" : "false");
        applySettings();
      });
      document.getElementById("vwap-band-count").addEventListener("change", event => {
        state.indicators.vwap.bandCount = Number(event.target.value);
        saveGlobalIndicatorSetting("vwapBandCount", String(state.indicators.vwap.bandCount));
        applySettings();
      });
      document.getElementById("vwap-band-color").addEventListener("input", event => {
        state.indicators.vwap.bandColor = event.target.value;
        saveGlobalIndicatorSetting("vwapBandColor", state.indicators.vwap.bandColor);
        applySettings();
      });
      for (const [id, stateKey, storageKey] of [
        ["vsa-volume-toggle", "visible", "vsaVolumeVisible"],
        ["vsa-volume-avg", "avg", "vsaVolumeAvg"],
        ["vsa-volume-labels", "labels", "vsaVolumeLabels"],
        ["vsa-volume-price-marks", "priceMarks", "vsaVolumePriceMarks"],
        ["vsa-volume-candle-colors", "candleColors", "vsaVolumeCandleColors"],
        ["vsa-volume-volume-colors", "volumeColors", "vsaVolumeVolumeColors"],
      ]) {
        document.getElementById(id).addEventListener("change", event => {
          state.indicators.vsaVolume[stateKey] = Boolean(event.target.checked);
          saveGlobalIndicatorSetting(
            storageKey,
            state.indicators.vsaVolume[stateKey] ? "true" : "false",
          );
          applySettings();
        });
      }
      document.getElementById("vsa-volume-render-hours").addEventListener("change", async event => {
        state.indicators.vsaVolume.renderHours = vsaVolumeRenderHours(event.target.value);
        saveGlobalIndicatorSetting(
          "vsaVolumeRenderHours",
          String(state.indicators.vsaVolume.renderHours),
        );
        applySettings();
        await load({ force: true, splitAnalysis: true, queueAnalysis: true });
      });
      bindManifestIndicatorControls();
      document.getElementById("strategy-mode-select").addEventListener("change", event => {
        state.strategyMode = event.target.value;
        saveInstrumentIndicatorSetting("strategyMode", state.strategyMode);
        load();
        applySettings();
      });
      document.getElementById("trade-setup-runtime-layout-full")?.addEventListener("click", () => setTradeSetupRuntimeLayout("full"));
      document.getElementById("trade-setup-runtime-layout-compact")?.addEventListener("click", () => setTradeSetupRuntimeLayout("compact"));
      document.getElementById("ny-range-toggle").addEventListener("change", event => {
        state.indicators.nyRange.enabled = event.target.checked;
        saveGlobalIndicatorSetting("nyRangeEnabled", state.indicators.nyRange.enabled ? "true" : "false");
        applySettings();
      });
      document.getElementById("ny-range-opacity").addEventListener("change", event => {
        state.indicators.nyRange.opacity = Number(event.target.value);
        saveGlobalIndicatorSetting("nyRangeOpacity", String(state.indicators.nyRange.opacity));
        applySettings();
      });
      document.getElementById("ny-range-style").addEventListener("change", event => {
        state.indicators.nyRange.style = event.target.value;
        saveGlobalIndicatorSetting("nyRangeStyle", state.indicators.nyRange.style);
        applySettings();
      });
      document.getElementById("ny-range-lines").addEventListener("change", event => {
        state.indicators.nyRange.priceLines = event.target.checked;
        saveGlobalIndicatorSetting("nyRangeLines", state.indicators.nyRange.priceLines ? "true" : "false");
        applySettings();
      });
      document.getElementById("prev-day-levels-toggle").addEventListener("change", event => {
        state.indicators.nyRange.prevDayLevels = event.target.checked;
        saveGlobalIndicatorSetting("prevDayLevelsEnabled", state.indicators.nyRange.prevDayLevels ? "true" : "false");
        applySettings();
      });
    }
    function setupWorkspaceDockControls() {
      const resize = value => {
        layout.workspaceDockWidth = workspaceDockRequestedWidth(value);
        refreshWorkspaceDockLayout();
      };
      const commit = () => {
        if (!setServerSettingValue("aef:workspaceDockWidth", String(layout.workspaceDockWidth))) {
          resize(serverSettingValue("aef:workspaceDockWidth") ?? 300);
        }
      };
      document.getElementById("workspace-dock-width")?.addEventListener("input", event => resize(event.target.value));
      document.getElementById("workspace-dock-width")?.addEventListener("change", commit);
      document.getElementById("workspace-dock-side")?.addEventListener("change", event => {
        if (!setServerSettingValue("aef:workspaceDockSide", event.target.value)) {
          refreshWorkspaceDockLayout();
          return;
        }
        layout.workspaceDockSide = event.target.value;
        refreshWorkspaceDockLayout();
      });
      const resizer = document.getElementById("workspace-dock-resizer");
      resizer?.addEventListener("pointerdown", event => {
        if (event.button !== 0) return;
        event.preventDefault();
        const startX = event.clientX;
        const startWidth = document.getElementById("workspace-dock").getBoundingClientRect().width;
        const direction = layout.workspaceDockSide === "left" ? 1 : -1;
        resizer.setPointerCapture(event.pointerId);
        resizer.classList.add("dragging");
        const move = next => {
          if (next.pointerId === event.pointerId) resize(startWidth + (next.clientX - startX) * direction);
        };
        const end = next => {
          if (next.pointerId !== event.pointerId) return;
          resizer.classList.remove("dragging");
          resizer.removeEventListener("pointermove", move);
          for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) resizer.removeEventListener(name, end);
          if (resizer.hasPointerCapture(event.pointerId)) resizer.releasePointerCapture(event.pointerId);
          commit();
        };
        resizer.addEventListener("pointermove", move);
        for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) resizer.addEventListener(name, end);
      });
      resizer?.addEventListener("keydown", event => {
        const direction = layout.workspaceDockSide === "left" ? 1 : -1;
        if (event.key === "ArrowLeft") resize(layout.workspaceDockWidth - 8 * direction);
        else if (event.key === "ArrowRight") resize(layout.workspaceDockWidth + 8 * direction);
        else if (event.key === "Home") resize(220);
        else if (event.key === "End") resize(640);
        else return;
        event.preventDefault();
        commit();
      });
      resizer?.addEventListener("dblclick", () => { resize(300); commit(); });
    }
