    function sleep(ms) {
      return new Promise(resolve => window.setTimeout(resolve, ms));
    }

    async function waitForBackendRestart(button, statusNode) {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await sleep(1000);
        try {
          await fetchJson("/api/health", { cache: "no-store", timeoutMs: 900 });
          if (statusNode) statusNode.textContent = "Backend restarted";
          if (button) {
            button.disabled = false;
            button.textContent = "Restart";
          }
          await loadSystemHealth();
          await load();
          connectQuoteStream(true);
          return;
        } catch (_) {
          // Backend is still restarting.
        }
      }
      if (statusNode) statusNode.textContent = "Restart status unknown";
      if (button) {
        button.disabled = false;
        button.textContent = "Restart";
      }
    }

    async function restartBackendContainer() {
      const button = document.getElementById("backend-restart");
      const statusNode = document.getElementById("backend-restart-status");
      if (button?.disabled) return;
      if (!window.confirm("Restart backend container?")) return;
      if (button) {
        button.disabled = true;
        button.textContent = "Restarting";
      }
      if (statusNode) statusNode.textContent = "Restarting backend";
      closeChartStream();
      closeQuoteStream();
      try {
        await fetchJson("/api/system/restart", { method: "POST", cache: "no-store", timeoutMs: 1200 });
      } catch (_) {
        // The restart can close the connection before the response reaches us.
      }
      waitForBackendRestart(button, statusNode);
    }

    async function reconnectTwsConnection() {
      const button = document.getElementById("tws-reconnect");
      const statusNode = document.getElementById("tws-reconnect-status");
      if (button?.disabled) return;
      if (button) {
        button.disabled = true;
        button.textContent = "Resetting";
      }
      if (statusNode) statusNode.textContent = "Resetting TWS sockets";
      const routes = quoteStreamRoutes();
      const payload = {
        instrument_ids: routes.map(([instrumentId]) => instrumentId),
        route_fingerprints: routes.map(([, routeFingerprint]) => routeFingerprint),
        ibkr_port: clamp(Number(state.settings.ibkrPort) || 7497, 1, 65535),
        timeout: 2.5,
      };
      try {
        const result = await fetchJson("/api/ibkr/reconnect", {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const failed = Object.entries(result.sessions || {})
          .filter(([, session]) => !session?.ok)
          .map(([name]) => name);
        if (statusNode) {
          statusNode.textContent = failed.length
            ? `TWS partial: ${failed.join(", ")}`
            : (result?.ok ? "TWS reconnected" : apiErrorMessage(result, "TWS reconnect failed"));
        }
      } catch (error) {
        if (statusNode) statusNode.textContent = `TWS reset failed: ${requestErrorMessage(error, "TWS reset failed")}`;
      } finally {
        closeQuoteStream();
        closeChartStream();
        await loadSystemHealth();
        await load();
        connectQuoteStream(true);
        connectChartStream(true);
        if (button) {
          button.disabled = false;
          button.textContent = "API RST";
        }
      }
    }

    async function requestNewIbkrGatewayLogin() {
      const button = document.getElementById("ibkr-gateway-login");
      const statusNode = document.getElementById("ibkr-gateway-login-status");
      if (button?.disabled) return;
      if (!window.confirm("Stop IB Gateway and request one new IB Key authorization?")) return;
      if (button) {
        button.disabled = true;
        button.textContent = "Starting";
      }
      if (statusNode) statusNode.textContent = "Starting one new IB Gateway login";
      closeQuoteStream();
      closeChartStream();
      try {
        const result = await fetchJson("/api/ibkr/gateway-login", {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirmation: "START_NEW_IBKR_LOGIN" }),
        });
        if (!result?.ok) {
          throw new Error(apiErrorMessage(result, "IB Gateway login request failed"));
        }
        if (statusNode) statusNode.textContent = "Approve the newest IB Key request";
        setUiNotice(
          "system",
          "IBKR_GATEWAY_LOGIN_REQUESTED",
          "IB Gateway is starting one new login. Approve the newest IB Key request.",
          { state: "waiting", routeScoped: false },
        );
      } catch (error) {
        const message = requestErrorMessage(error, "IB Gateway login request failed");
        if (statusNode) statusNode.textContent = message;
        showBrowserToast(message);
      } finally {
        window.setTimeout(() => {
          const currentButton = document.getElementById("ibkr-gateway-login");
          if (currentButton) {
            currentButton.disabled = false;
            currentButton.textContent = "New login";
          }
        }, 30000);
      }
    }

    async function setServerSleepMode(sleeping) {
      const button = document.getElementById("server-sleep-toggle");
      const statusNode = document.getElementById("server-sleep-status");
      if (button?.disabled) return;
      if (button) {
        button.disabled = true;
        button.textContent = sleeping ? "Sleeping" : "Waking";
      }
      if (statusNode) statusNode.textContent = sleeping ? "Pausing IBKR polling" : "Resuming IBKR polling";
      if (sleeping) {
        closeQuoteStream();
        closeChartStream();
        if (state.requests.market.abort) state.requests.market.abort.abort();
        state.requests.market.loading = false;
        state.requests.market.pending = false;
        state.requests.market.pendingOptions = null;
        state.requests.market.key = "";
      }
      try {
        const result = await fetchJson(`/api/system/${sleeping ? "sleep" : "wake"}`, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: sleeping ? "weekend manual sleep" : "manual wake" }),
        });
        state.serverSleeping = Boolean(result?.sleep?.sleeping);
        if (!result?.ok && result?.error) {
          setUiNotice(
            "system",
            "SERVER_SLEEP_TRANSITION_FAILED",
            apiErrorMessage(result, "Server sleep request reported a disconnect error."),
            { state: "error", routeScoped: false },
          );
        } else {
          setUiNotice(
            "system",
            state.serverSleeping ? "SERVER_SLEEPING" : "SERVER_AWAKE",
            state.serverSleeping ? "Server sleeping: IBKR polling paused." : "Server awake: IBKR polling resumed.",
            { state: state.serverSleeping ? "sleeping" : "ready", routeScoped: false },
          );
        }
        await loadSystemHealth();
        if (!state.serverSleeping) {
          await load({ force: true, allowWhenSleeping: true });
          connectQuoteStream(true);
          connectChartStream(true);
        } else if (state.snapshot) {
          renderPanel(state.snapshot);
          renderInstruments();
          renderCharts(state.snapshot);
        }
      } catch (error) {
        if (statusNode) statusNode.textContent = `Sleep toggle failed: ${requestErrorMessage(error, "sleep toggle failed")}`;
      } finally {
        if (button) button.disabled = false;
        renderSystemHealth();
      }
    }

    async function setTelegramInteractiveEnabled(enabled) {
      const statusNode = document.getElementById("telegram-interactive-status");
      const toggle = document.getElementById("telegram-interactive-toggle");
      if (statusNode) statusNode.textContent = enabled ? "Enabling Telegram bot..." : "Disabling Telegram bot...";
      if (toggle) toggle.disabled = true;
      try {
        const data = await fetchJson("/api/alerts/telegram/interactive", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: Boolean(enabled) }),
        });
        if (!data.ok) throw new Error(apiErrorMessage(data, "Telegram bot update failed"));
        await loadSystemHealth();
      } catch (error) {
        const message = requestErrorMessage(error, "Telegram bot update failed");
        if (statusNode) statusNode.textContent = message;
        if (toggle) toggle.checked = !enabled;
        showBrowserToast(message);
      } finally {
        if (toggle) toggle.disabled = !state.systemHealth?.telegram?.interactive?.configured;
      }
    }

    function savePresetSideTab(tab) {
      state.sideTab = tab;
      workspaceSet("sideTab", tab);
      if (typeof applySideTabs === "function") applySideTabs();
      syncWorkspacePageState();
    }

    function savePresetBool(key, value) {
      if (key === "gexFocusMode") {
        workspaceSetBool(key, value);
        return;
      }
      setServerSettingValue(key, value ? "true" : "false");
    }

    function renderAfterPreset() {
      applySettings();
      applyLayout();
      if (state.snapshot) renderCharts(state.snapshot);
      renderAlertManager();
    }

    async function applyViewPresetSelection(value, options = {}) {
      const preset = normalizeViewPreset(value);
      const gexProfileMode = preset === "gex-expiry" ? "expiry" : "ladder";
      const gexZoneMode = preset === "gex-strike" ? "strike" : "zones";
      const basePreset = preset.startsWith("gex") ? "gex" : preset;
      state.settings.viewPreset = basePreset;
      workspaceSet("viewPreset", basePreset);

      if (basePreset === "gex") {
        state.settings.gexFocusMode = true;
        savePresetBool("gexFocusMode", true);
        state.indicators.gexContext.profile = true;
        state.indicators.gexContext.profileStyle = gexProfileMode;
        state.indicators.gexContext.zoneStyle = gexZoneMode;
        state.indicators.gexContext.zones = gexZoneMode === "zones";
        saveInstrumentIndicatorSetting("gexContextProfile", "true", "gex");
        saveInstrumentIndicatorSetting("gexContextProfileStyle", gexProfileMode, "gex");
        saveInstrumentIndicatorSetting("gexContextZoneStyle", gexZoneMode, "gex");
        saveInstrumentIndicatorSetting("gexContextZones", state.indicators.gexContext.zones ? "true" : "false", "gex");
        await applyIndicatorModeSelection("gex", options);
        savePresetSideTab("indicators");
        return;
      } else if (basePreset === "alerts") {
        state.settings.gexFocusMode = false;
        savePresetBool("gexFocusMode", false);
        state.indicators.ema233.allEnabled = true;
        state.indicators.ema233.enabled = true;
        saveGlobalIndicatorSetting("emaAllEnabled", "true");
        saveGlobalIndicatorSetting("ema233Enabled", "true");
        savePresetSideTab("alerts");
      } else if (basePreset === "mobile") {
        state.settings.gexFocusMode = false;
        savePresetBool("gexFocusMode", false);
        layout.sideWidth = 300;
        layout.volumeHeight = 110;
        view.barsVisible = clamp(Math.round(Number(view.barsVisible) || 80), 48, 120);
        setServerSettingValue("aef:sideWidth", String(layout.sideWidth));
        setServerSettingValue("aef:volumeHeight", String(layout.volumeHeight));
        saveBarsVisibleForCurrent();
        savePresetSideTab(state.sideTab || "go");
      } else {
        state.settings.gexFocusMode = false;
        savePresetBool("gexFocusMode", false);
        layout.sideWidth = Math.max(Number(layout.sideWidth) || 360, 360);
        layout.volumeHeight = Math.max(Number(layout.volumeHeight) || 160, 160);
        setServerSettingValue("aef:sideWidth", String(layout.sideWidth));
        setServerSettingValue("aef:volumeHeight", String(layout.volumeHeight));
      }

      renderAfterPreset();
      syncWorkspacePageState();
    }

    async function applyIndicatorModeSelection(value, options = {}) {
      const mode = normalizeIndicatorMode(value);
      activeIndicatorMode = mode;
      state.settings.indicatorMode = mode;
      state.settings.gexMode = mode === "gex";
      setIndicatorModeForInstrument(state.instrumentId, mode);
      loadIndicatorSettingsForCurrent();
      if (state.settings.gexMode) await loadGexContext({ refresh: false, force: true });
      clearUiNotice("market");
      clearUiNotice("history");
      if (options.reload !== false) load({ force: true });
      renderAfterPreset();
      syncWorkspacePageState();
    }
