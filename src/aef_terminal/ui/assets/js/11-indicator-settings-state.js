    function saveGlobalIndicatorSetting(key, value) {
      setServerSettingValue(globalIndicatorSettingKey(key), String(value));
    }

    function saveInstrumentIndicatorSetting(key, value, mode = activeIndicatorMode) {
      const storageKey = instrumentIndicatorSettingKey(key, state.instrumentId, mode);
      if (!storageKey) return;
      setServerSettingValue(storageKey, String(value));
    }

    function gexFocusMode() {
      return Boolean(state?.settings?.gexFocusMode);
    }

    function gexLayerVisible() {
      return Boolean(state?.indicators?.gexContext?.enabled || gexFocusMode());
    }

    function indicatorRegistryEntries() {
      return Object.entries(window.INDICATOR_REGISTRY_MANIFEST || {});
    }

    function managedIndicatorEntryFromSpec(id, spec) {
      const ui = spec?.ui || {};
      if (!ui.state_key || !ui.calc_key || !ui.process_control_id) return null;
      return {
        id,
        group: ui.state_key,
        calcKey: ui.calc_key,
        visibleKey: ui.visible_key || "",
        apiVisibleKey: ui.api_visible_key || "",
        chartId: ui.chart_control_id || "",
        calcId: ui.process_control_id,
        processEffectRef: ui.process_effect_ref || "",
        global: ui.settings_scope === "global",
        uiOnly: spec.module_type === "ui-only",
        moduleType: spec.module_type || "signal",
        managerOrder: numericValueOrFallback(ui.manager_order, 500),
        runtimeOrder: numericValueOrFallback(ui.runtime_order, 500),
        defaultCalc: ui.default_calc !== false,
        defaultVisible: ui.default_visible !== false,
      };
    }

    const MANAGED_INDICATOR_UI = [
      ...indicatorRegistryEntries()
        .map(([id, spec]) => managedIndicatorEntryFromSpec(id, spec))
        .filter(Boolean),
    ].sort((a, b) => (
      numericValueOrFallback(a.managerOrder, 500)
      - numericValueOrFallback(b.managerOrder, 500)
    ));

    function readIndicatorSettingBool(key, fallback, global = false) {
      if (global) return storedGlobalIndicatorBool(key, fallback);
      return storedInstrumentIndicatorBool(key, fallback, state.instrumentId);
    }

    function saveIndicatorSettingBool(key, value, global = false) {
      if (global) saveGlobalIndicatorSetting(key, value ? "true" : "false");
      else saveInstrumentIndicatorSetting(key, value ? "true" : "false");
    }

    function ensureManagedIndicatorStateGroups() {
      MANAGED_INDICATOR_UI.forEach(entry => {
        if (!entry.group || state.indicators?.[entry.group]) return;
        state.indicators[entry.group] = {
          enabled: entry.defaultCalc,
          visible: entry.defaultVisible,
        };
      });
    }

    function loadIndicatorCalcAndVisible(calcKey, visibleKey, defaults = { calc: true, visible: true }, global = false) {
      const calc = readIndicatorSettingBool(calcKey, defaults.calc, global);
      const visibleStored = readIndicatorSettingBool(visibleKey, defaults.visible, global);
      return { enabled: calc, visible: calc ? visibleStored : false };
    }

    function loadManagedIndicatorCalcAndVisibleForCurrent() {
      MANAGED_INDICATOR_UI.forEach(entry => {
        const group = state.indicators?.[entry.group];
        if (!group) return;
        Object.assign(group, loadIndicatorCalcAndVisible(
          entry.calcKey,
          entry.visibleKey,
          { calc: entry.defaultCalc, visible: entry.defaultVisible },
          entry.global,
        ));
      });
    }

    function storedManifestControlValue(control) {
      const fallback = control.default;
      const global = control.scope === "global";
      if (!global && control.scope !== "instrument") {
        throw new Error(`INDICATOR_CONTROL_SCOPE_INVALID scope=${String(control.scope)}`);
      }
      const storageKey = control.storage_key;
      if (control.control_type === "toggle") {
        if (global) return storedGlobalIndicatorBool(storageKey, Boolean(fallback));
        return storedInstrumentIndicatorBool(storageKey, Boolean(fallback), state.instrumentId);
      }
      if (control.control_type === "number") {
        const raw = global
          ? storedGlobalIndicatorNumber(storageKey, Number(fallback))
          : storedInstrumentIndicatorNumber(storageKey, Number(fallback), state.instrumentId);
        const minimum = Number(control.minimum);
        const maximum = Number(control.maximum);
        const low = Number.isFinite(minimum) ? minimum : -Infinity;
        const high = Number.isFinite(maximum) ? maximum : Infinity;
        return clamp(Number.isFinite(Number(raw)) ? Number(raw) : Number(fallback), low, high);
      }
      const value = global
        ? storedGlobalIndicatorString(storageKey, String(fallback ?? ""))
        : storedInstrumentIndicatorString(
          storageKey,
          String(fallback ?? ""),
          state.instrumentId,
        );
      const options = Array.isArray(control.options) ? control.options : [];
      return options.length && !options.includes(value) ? fallback : value;
    }

    function applyManifestDerivedStateRef(ref, group, value = undefined) {
      if (!group) return false;
      if (ref === "table_position_state") {
        group.table = (value ?? group.tablePosition) !== "off";
        return true;
      }
      return false;
    }

    function syncManifestIndicatorDerivedState(spec, group) {
      (spec?.ui?.derived_state_refs || []).forEach(ref => applyManifestDerivedStateRef(ref, group));
    }

    function loadManifestIndicatorControlsForCurrent() {
      indicatorRegistryEntries().forEach(([indicatorId, spec]) => {
        const ui = spec?.ui || {};
        const group = state.indicators?.[ui.state_key];
        if (!group) return;
        (spec.controls || []).forEach(control => {
          group[control.state_key || control.key] = storedManifestControlValue(control);
        });
        syncManifestIndicatorDerivedState(spec, group);
      });
    }

    function coerceIndicatorVisibility(group) {
      if (!group || group.enabled === false) group.visible = false;
      return group;
    }

    function syncManagedIndicatorChartToggle(entry) {
      const group = state.indicators[entry.group];
      if (!group) return;
      coerceIndicatorVisibility(group);
      const calcInput = document.getElementById(entry.calcId);
      const chartInput = document.getElementById(entry.chartId);
      const calcOn = group.enabled !== false;
      if (calcInput) calcInput.checked = calcOn;
      if (entry.calcId && entry.calcId === entry.chartId) return;
      if (entry.visibleKey && chartInput) {
        chartInput.disabled = !calcOn;
        chartInput.checked = calcOn && group.visible !== false;
        chartInput.closest("label.toggle")?.classList.toggle("indicator-show-disabled", !calcOn);
      }
    }

    function syncAllManagedIndicatorChartToggles() {
      MANAGED_INDICATOR_UI.forEach(syncManagedIndicatorChartToggle);
    }
    window.syncAllManagedIndicatorChartToggles = syncAllManagedIndicatorChartToggles;

    function loadIndicatorSettingsForCurrent() {
      activeIndicatorMode = storedIndicatorMode(state.instrumentId);
      state.settings.indicatorMode = activeIndicatorMode;
      state.settings.gexMode = activeIndicatorMode === "gex";
      state.indicators.ema233.allEnabled = storedGlobalIndicatorBool("emaAllEnabled", true);
      state.indicators.ema233.enabled = storedGlobalIndicatorBool("ema233Enabled", true);
      state.indicators.ema233.ema20Enabled = storedGlobalIndicatorBool("ema20Enabled", true);
      state.indicators.ema233.ema50Enabled = storedGlobalIndicatorBool("ema50Enabled", false);
      state.indicators.ema233.ema20Color = storedGlobalIndicatorString("ema20Color", css("--ema20") || "#facc15");
      state.indicators.ema233.ema50Color = storedGlobalIndicatorString("ema50Color", "");
      state.indicators.ema233.color = storedGlobalIndicatorString("ema233Color", css("--ema233") || "#38bdf8");
      state.indicators.ema233.style = storedGlobalIndicatorString("ema233Style", "solid");
      state.indicators.ema233.width = storedGlobalIndicatorNumber("ema233Width", 2);
      state.indicators.ema233.alertRearmMinutes = storedGlobalIndicatorNumber("ema233AlertRearmMinutes", 60);
      state.indicators.vwap.enabled = storedGlobalIndicatorBool("vwapEnabled", true);
      state.indicators.vwap.style = storedGlobalIndicatorString("vwapStyle", "dashed");
      state.indicators.vwap.width = storedGlobalIndicatorNumber("vwapWidth", 1);
      state.indicators.vwap.color = storedGlobalIndicatorString("vwapColor", css("--vwap") || "#22c55e");
      state.indicators.vwap.bands = storedGlobalIndicatorBool("vwapBands", true);
      state.indicators.vwap.bandCount = storedGlobalIndicatorNumber("vwapBandCount", 2);
      state.indicators.vwap.bandColor = storedGlobalIndicatorString("vwapBandColor", "");
      state.indicators.vsaVolume.visible = storedGlobalIndicatorBool("vsaVolumeVisible", true);
      state.indicators.vsaVolume.avg = storedGlobalIndicatorBool("vsaVolumeAvg", true);
      state.indicators.vsaVolume.labels = storedGlobalIndicatorBool("vsaVolumeLabels", true);
      state.indicators.vsaVolume.priceMarks = storedGlobalIndicatorBool("vsaVolumePriceMarks", true);
      state.indicators.vsaVolume.candleColors = storedGlobalIndicatorBool("vsaVolumeCandleColors", true);
      state.indicators.vsaVolume.volumeColors = storedGlobalIndicatorBool("vsaVolumeVolumeColors", false);
      state.indicators.vsaVolume.renderHours = storedGlobalIndicatorNumber("vsaVolumeRenderHours", 6);
      state.indicators.globalDefaults.emaPullback = storedGlobalIndicatorNumberInRange("globalEmaPullback", 20, 5, 300);
      state.indicators.globalDefaults.emaFast = storedGlobalIndicatorNumberInRange("globalEmaFast", 21, 5, 300);
      state.indicators.globalDefaults.emaSlow = storedGlobalIndicatorNumberInRange("globalEmaSlow", 55, 10, 400);
      state.indicators.globalDefaults.emaMagnet = storedGlobalIndicatorNumberInRange("globalEmaMagnet", 233, 50, 1000);
      state.indicators.nyRange.enabled = storedGlobalIndicatorBool("nyRangeEnabled", true);
      state.indicators.nyRange.opacity = storedGlobalIndicatorNumber("nyRangeOpacity", 14);
      state.indicators.nyRange.style = storedGlobalIndicatorString("nyRangeStyle", "dotted");
      state.indicators.nyRange.priceLines = storedGlobalIndicatorBool("nyRangeLines", true);
      state.indicators.nyRange.prevDayLevels = storedGlobalIndicatorBool("prevDayLevelsEnabled", false);
      ensureManagedIndicatorStateGroups();
      loadManagedIndicatorCalcAndVisibleForCurrent();
      loadManifestIndicatorControlsForCurrent();
      state.indicators.optionTargets.enabled = storedInstrumentIndicatorBool(
        "optionTargetsEnabled",
        true,
        state.instrumentId,
      );
      state.indicators.optionTargets.fastStatus = storedInstrumentIndicatorBool(
        "optionTargetsFastStatus",
        true,
        state.instrumentId,
      );
      state.indicators.optionTargets.pulse = storedInstrumentIndicatorBool(
        "optionTargetsPulse",
        true,
        state.instrumentId,
      );
      state.indicators.gexContext.enabled = storedInstrumentIndicatorBool("gexContextEnabled", false, state.instrumentId);
      const gexContextMode = storedInstrumentIndicatorString("gexContextMode", "request", state.instrumentId);
      state.indicators.gexContext.mode = ["request", "live"].includes(gexContextMode)
        ? gexContextMode
        : "request";
      state.indicators.gexContext.zones = storedInstrumentIndicatorBool("gexContextZones", true, state.instrumentId);
      state.indicators.gexContext.zoneStyle = storedInstrumentIndicatorString("gexContextZoneStyle", state.indicators.gexContext.zones ? "lines" : "off", state.instrumentId);
      state.indicators.gexContext.profile = storedInstrumentIndicatorBool("gexContextProfile", true, state.instrumentId);
      state.indicators.gexContext.profileStyle = storedInstrumentIndicatorString("gexContextProfileStyle", "mini", state.instrumentId);
      state.indicators.gexContext.historyView = storedGexHistoryView(state.instrumentId);
      const displayLevels = storedInstrumentIndicatorNumber(
        "gexContextDisplayLevels",
        GEX_DEFAULT_DISPLAY_LEVEL_COUNT,
        state.instrumentId,
      );
      state.indicators.gexContext.displayLevels = GEX_DISPLAY_LEVEL_COUNTS.includes(displayLevels)
        ? displayLevels
        : GEX_DEFAULT_DISPLAY_LEVEL_COUNT;
      state.indicators.gexContext.dynamicsVisible = storedInstrumentIndicatorBool(
        "gexDynamicsVisible",
        false,
        state.instrumentId,
      );
      const strategyMode = storedInstrumentIndicatorString(
        "strategyMode",
        "balanced",
        state.instrumentId,
      );
      state.strategyMode = STRATEGY_MODE_CODES.includes(strategyMode) ? strategyMode : "balanced";
      syncAllManagedIndicatorChartToggles();
      if (typeof syncIndicatorModuleLifecycles === "function") {
        void syncIndicatorModuleLifecycles();
      }
    }
