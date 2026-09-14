function setupGexSettingsBindings() {
      let schedulerSaveInFlight = false;
      let queuedSchedulerSettings = null;
      let authoritativeSchedulerSettings = null;

      function currentGexSchedulerSettings() {
        return {
          enabled: Boolean(state.settings.gexSchedulerEnabled),
          instrumentIds: Array.isArray(state.settings.gexSchedulerInstrumentIds)
            ? [...state.settings.gexSchedulerInstrumentIds]
            : [],
        };
      }

      function schedulerSettingsFromPayload(payload) {
        if (
          typeof payload?.enabled !== "boolean"
          || !Array.isArray(payload?.instrument_ids)
          || !payload.instrument_ids.every(instrumentId => typeof instrumentId === "string" && instrumentId)
        ) return null;
        return {
          enabled: payload.enabled,
          instrumentIds: [...payload.instrument_ids],
        };
      }

      function projectGexSchedulerSettings(settings) {
        state.settings.gexSchedulerEnabled = Boolean(settings?.enabled);
        state.settings.gexSchedulerInstrumentIds = Array.isArray(settings?.instrumentIds)
          ? [...settings.instrumentIds]
          : [];
        renderGexSchedulerInstrumentOptions();
        applySettings();
      }

      function publishGexSchedulerError(code, detail) {
        publishUiFeedback({
          kind: "gex_scheduler",
          category: "settings",
          source: "gex_scheduler",
          code,
          status: "save_failed",
          title: "GEX auto refresh",
          detail,
          severity: "warning",
          tone: "error",
          target_id: "gex-scheduler-instrument-ids",
          toast: true,
          attention: false,
        });
      }

      function setGexSchedulerSaveBusy(busy) {
        const toggle = document.getElementById("gex-scheduler-toggle");
        const select = document.getElementById("gex-scheduler-instrument-ids");
        for (const control of [toggle, select]) {
          if (!control) continue;
          control.setAttribute("aria-busy", busy ? "true" : "false");
          control.classList.toggle("saving", busy);
        }
      }

      async function drainGexSchedulerSettingsSaves() {
        if (schedulerSaveInFlight) return;
        schedulerSaveInFlight = true;
        setGexSchedulerSaveBusy(true);
        try {
          while (queuedSchedulerSettings) {
            const requested = queuedSchedulerSettings;
            queuedSchedulerSettings = null;
            try {
              const payload = await fetchJson("/api/gex/scheduler", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  enabled: requested.enabled,
                  instrument_ids: requested.instrumentIds,
                }),
              });
              const saved = payload?.ok === true ? schedulerSettingsFromPayload(payload) : null;
              if (!saved || saved.instrumentIds.length === 0) {
                const code = payload?.error?.code || "GEX_SCHEDULER_SAVE_FAILED";
                publishGexSchedulerError(
                  code,
                  apiErrorMessage(payload, "GEX scheduler settings could not be saved"),
                );
              } else {
                authoritativeSchedulerSettings = saved;
              }
            } catch (error) {
              publishGexSchedulerError(
                requestErrorCode(error) || "GEX_SCHEDULER_SAVE_FAILED",
                requestErrorMessage(error, "GEX scheduler settings could not be saved"),
              );
            }

            if (queuedSchedulerSettings) continue;
            projectGexSchedulerSettings(authoritativeSchedulerSettings);
            await loadSystemHealth({ force: true });
            const healthSettings = schedulerSettingsFromPayload(state.systemHealth?.gex?.scheduler);
            if (healthSettings) authoritativeSchedulerSettings = healthSettings;
            else projectGexSchedulerSettings(authoritativeSchedulerSettings);
            if (queuedSchedulerSettings) projectGexSchedulerSettings(queuedSchedulerSettings);
          }
        } finally {
          schedulerSaveInFlight = false;
          authoritativeSchedulerSettings = null;
          setGexSchedulerSaveBusy(false);
          applySettings();
        }
      }

      function saveGexSchedulerSettings(previousSettings) {
        if (!authoritativeSchedulerSettings) authoritativeSchedulerSettings = previousSettings;
        queuedSchedulerSettings = currentGexSchedulerSettings();
        drainGexSchedulerSettingsSaves();
      }

      document.getElementById("gex-scheduler-toggle").addEventListener("change", event => {
        const previousSettings = currentGexSchedulerSettings();
        const enabled = Boolean(event.target.checked);
        if (enabled && previousSettings.instrumentIds.length === 0) {
          state.settings.gexSchedulerEnabled = false;
          event.target.checked = false;
          applySettings();
          publishGexSchedulerError(
            "GEX_SCHEDULER_INSTRUMENT_REQUIRED",
            "Select at least one instrument in Auto refresh instruments before enabling AUTO.",
          );
          return;
        }
        state.settings.gexSchedulerEnabled = enabled;
        saveGexSchedulerSettings(previousSettings);
      });
      document.getElementById("gex-scheduler-instrument-ids").addEventListener("change", event => {
        const previousSettings = currentGexSchedulerSettings();
        const instrumentIds = [...event.target.selectedOptions]
          .map(option => option.value);
        if (instrumentIds.length === 0) {
          projectGexSchedulerSettings(previousSettings);
          publishGexSchedulerError(
            "GEX_SCHEDULER_INSTRUMENT_REQUIRED",
            "Keep at least one instrument selected for Auto refresh instruments.",
          );
          return;
        }
        state.settings.gexSchedulerInstrumentIds = instrumentIds;
        renderGexSchedulerInstrumentOptions();
        saveGexSchedulerSettings(previousSettings);
      });
      document.getElementById("gex-context-toggle").addEventListener("change", async event => {
        state.indicators.gexContext.enabled = event.target.checked;
        saveInstrumentIndicatorSetting("gexContextEnabled", state.indicators.gexContext.enabled ? "true" : "false");
        if (gexLayerVisible()) {
          await loadGexContext({ refresh: false, force: true });
        }
        else {
          if (gexLiveModeActive()) await stopGexLiveContext();
          clearGexContext();
          if (state.snapshot) renderCharts(state.snapshot);
        }
        if (state.snapshot && snapshotMatchesCurrentRoute()) {
          reloadCurrentAnalysisOnly("gex-context-toggle", { renderImmediate: false });
        }
        applySettings();
      });
      document.getElementById("gex-dynamics-toggle").addEventListener("change", event => {
        state.indicators.gexContext.dynamicsVisible = event.target.checked;
        saveInstrumentIndicatorSetting(
          "gexDynamicsVisible",
          state.indicators.gexContext.dynamicsVisible ? "true" : "false",
        );
        if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
        applySettings();
      });
      document.getElementById("gex-display-levels")?.addEventListener("change", event => {
        const requested = Number(event.target.value);
        state.indicators.gexContext.displayLevels = GEX_DISPLAY_LEVEL_COUNTS.includes(requested)
          ? requested
          : GEX_DEFAULT_DISPLAY_LEVEL_COUNT;
        event.target.value = String(state.indicators.gexContext.displayLevels);
        saveInstrumentIndicatorSetting("gexContextDisplayLevels", state.indicators.gexContext.displayLevels);
        if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
        applySettings();
      });
      if (gexLiveModeRequested() && gexLayerVisible()) connectGexLiveStream();
    }
