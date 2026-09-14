    const INDICATOR_MODE_BOOL_DEFAULTS = {
      regular: {
        gexContextEnabled: false,
        gexDynamicsVisible: false,
      },
      gex: {
        emaAllEnabled: true,
        ema20Enabled: true,
        ema50Enabled: false,
        ema233Enabled: true,
        vwapEnabled: true,
        vwapBands: false,
        vsaVolumeVisible: true,
        vsaVolumeAvg: true,
        vsaVolumeLabels: true,
        vsaVolumePriceMarks: true,
        vsaVolumeCandleColors: true,
        vsaVolumeVolumeColors: false,
        gexContextEnabled: true,
        gexContextZones: true,
        gexContextProfile: true,
        gexDynamicsVisible: false,
        nyRangeEnabled: false,
        prevDayLevelsEnabled: false,
      },
    };

    function normalizeIndicatorMode(value) {
      return String(value || "").toLowerCase() === "gex" ? "gex" : "regular";
    }

    function indicatorModeStorageKey(instrumentId = initialInstrumentId) {
      const exactInstrumentId = typeof instrumentId === "string" ? instrumentId : "";
      if (!exactInstrumentId) return "";
      return `aef:instrument:${JSON.stringify([exactInstrumentId])}:indicatorMode`;
    }

    function storedIndicatorMode(instrumentId = initialInstrumentId) {
      const key = indicatorModeStorageKey(instrumentId);
      if (!key) return "regular";
      const value = serverSettingValue(key);
      return normalizeIndicatorMode(value);
    }

    function setIndicatorModeForInstrument(instrumentId, mode) {
      const key = indicatorModeStorageKey(instrumentId);
      if (!key) return;
      setServerSettingValue(key, normalizeIndicatorMode(mode));
    }

    let activeIndicatorMode = storedIndicatorMode(initialInstrumentId);

    function modeBoolDefault(key, fallback, mode = activeIndicatorMode) {
      const defaults = INDICATOR_MODE_BOOL_DEFAULTS[normalizeIndicatorMode(mode)] || {};
      return Object.hasOwn(defaults, key) ? Boolean(defaults[key]) : fallback;
    }

    function rangeStepsFor(interval) {
      return RANGE_STEPS[interval] || ["5d", "31d"];
    }

    function normalizeSignalsRange(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return ["1d", "2d", "3d", "7d"].includes(normalized) ? normalized : "2d";
    }

    function normalizeSignalDensity(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return ["focus", "research", "minimal"].includes(normalized) ? normalized : "focus";
    }

    function normalizeHealthVisualizer(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return ["candles", "snake", "off"].includes(normalized) ? normalized : "candles";
    }

    function normalizeMotionPreference(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return ["system", "reduced", "off"].includes(normalized) ? normalized : "system";
    }

    function uiMotionReduced() {
      const preference = normalizeMotionPreference(state?.settings?.motionPreference);
      if (preference === "reduced" || preference === "off") return true;
      return Boolean(
        typeof window !== "undefined"
        && window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches
      );
    }

    function uiMotionEnabled() {
      return normalizeMotionPreference(state?.settings?.motionPreference) !== "off" && !uiMotionReduced();
    }

    function loadHealthVisualizerSetting() {
      return normalizeHealthVisualizer(serverSettingValue("aef:healthVisualizer"));
    }

    function normalizeUiShape(value) {
      const normalized = String(value || "").toLowerCase();
      return ["flat", "tv-flat", "tv-black", "tv-black-flat"].includes(normalized) ? normalized : "rounded";
    }

    function normalizeChartViewMode(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return normalized === "advisor" ? "advisor" : "classic";
    }

    function normalizeCursorMode(value) {
      return String(value || "").trim().toLowerCase() === "informative"
        ? "informative"
        : "normal";
    }

    function normalizeWatchlistPresentation(value) {
      if (
        !value
        || typeof value !== "object"
        || Array.isArray(value)
        || Object.keys(value).length !== 2
        || !Object.hasOwn(value, "display_mode")
        || !Object.hasOwn(value, "revision")
        || !["classic", "trend"].includes(value.display_mode)
        || !Number.isSafeInteger(value.revision)
        || value.revision < 0
      ) return null;
      return { display_mode: value.display_mode, revision: value.revision };
    }

    function canvasExpensiveEffectsEnabled() {
      return !(typeof chartInteractionQualityActive === "function" && chartInteractionQualityActive());
    }

    function canvasShadowBlur(value) {
      return canvasExpensiveEffectsEnabled() ? value : 0;
    }

    function normalizeIndicatorLabelStyle(value) {
      const normalized = String(value || "").trim().toLowerCase();
      return ["text", "box"].includes(normalized) ? normalized : "text";
    }

    function normalizeViewPreset(value) {
      const preset = String(value || "trading").toLowerCase();
      return ["trading", "gex", "gex-strike", "gex-expiry", "alerts", "mobile"].includes(preset) ? preset : "trading";
    }

    function defaultRangeFor(interval) {
      const preferred = DEFAULT_RANGE_BY_INTERVAL[interval];
      return rangeStepsFor(interval).includes(preferred) ? preferred : rangeStepsFor(interval)[0];
    }

    function initialRangeFor(interval, stored, options = {}) {
      const steps = rangeStepsFor(interval);
      const fallback = defaultRangeFor(interval);
      const storedIndex = steps.indexOf(stored);
      if (storedIndex < 0) return fallback;
      if (options.allowExpanded) return stored;
      const defaultIndex = steps.indexOf(fallback);
      return defaultIndex >= 0 && storedIndex > defaultIndex ? fallback : stored;
    }

    function rangeStorageKey(instrumentId, interval, slot = activeWorkspaceSlot) {
      const exactInstrumentId = typeof instrumentId === "string" ? instrumentId : "";
      if (!exactInstrumentId) return "";
      return workspaceKey(`range:${JSON.stringify([exactInstrumentId, String(interval || "")])}`, slot);
    }

    function barsVisibleStorageKey(instrumentId, interval, slot = activeWorkspaceSlot) {
      const exactInstrumentId = typeof instrumentId === "string" ? instrumentId : "";
      if (!exactInstrumentId) return "";
      return workspaceKey(`barsVisible:${JSON.stringify([exactInstrumentId, String(interval || "")])}`, slot);
    }

    function storedBarsVisibleForInstrument(instrumentId, interval) {
      const key = barsVisibleStorageKey(instrumentId, interval);
      const value = Number(serverSettingValue(key));
      return Number.isFinite(value)
        ? clamp(Math.round(value), 24, MAX_BARS_VISIBLE)
        : DEFAULT_BARS_VISIBLE;
    }

    function storedRangeForInstrument(instrumentId, interval) {
      const key = rangeStorageKey(instrumentId, interval);
      const stored = serverSettingValue(key);
      return initialRangeFor(interval, stored);
    }

    function storedBool(key, fallback) {
      const value = serverSettingValue(key);
      return value === null ? fallback : value !== "false";
    }

    function storedWorkspaceBool(key, fallback) {
      const value = workspaceValue(key);
      return value === null ? fallback : value !== "false";
    }
    function globalIndicatorSettingKey(key) {
      return `aef:indicator:global:${key}`;
    }
    function storedGlobalIndicatorBool(key, fallback) {
      const value = serverSettingValue(globalIndicatorSettingKey(key));
      return value === null ? fallback : value !== "false";
    }
    function numericValueOrFallback(raw, fallback) {
      if (
        raw === null
        || raw === undefined
        || (typeof raw === "string" && raw.trim() === "")
      ) return fallback;
      const value = Number(raw);
      return Number.isFinite(value) ? value : fallback;
    }
    function storedGlobalIndicatorNumber(key, fallback) {
      return numericValueOrFallback(
        serverSettingValue(globalIndicatorSettingKey(key)),
        fallback,
      );
    }
    function storedGlobalIndicatorNumberInRange(key, fallback, low, high) {
      const value = numericValueOrFallback(
        serverSettingValue(globalIndicatorSettingKey(key)),
        Number.NaN,
      );
      if (!Number.isFinite(value)) return fallback;
      if (value < low || value > high) return fallback;
      return value;
    }
    function storedGlobalIndicatorString(key, fallback) {
      const value = serverSettingValue(globalIndicatorSettingKey(key));
      return value === null ? fallback : value;
    }
    function instrumentIndicatorSettingKey(key, instrumentId = initialInstrumentId, mode = activeIndicatorMode) {
      const exactInstrumentId = typeof instrumentId === "string" ? instrumentId : "";
      if (!exactInstrumentId) return "";
      return `aef:instrument:${JSON.stringify([exactInstrumentId, normalizeIndicatorMode(mode)])}:indicator:${key}`;
    }

    function storedInstrumentIndicatorNumber(key, fallback, instrumentId = initialInstrumentId) {
      const storageKey = instrumentIndicatorSettingKey(key, instrumentId);
      if (!storageKey) return fallback;
      return numericValueOrFallback(serverSettingValue(storageKey), fallback);
    }

    function storedInstrumentIndicatorBool(key, fallback, instrumentId = initialInstrumentId) {
      const storageKey = instrumentIndicatorSettingKey(key, instrumentId);
      if (!storageKey) return modeBoolDefault(key, fallback);
      const value = serverSettingValue(storageKey);
      return value === null ? modeBoolDefault(key, fallback) : value !== "false";
    }

    function storedInstrumentIndicatorString(key, fallback, instrumentId = initialInstrumentId) {
      const storageKey = instrumentIndicatorSettingKey(key, instrumentId);
      if (!storageKey) return fallback;
      const value = serverSettingValue(storageKey);
      return value === null ? fallback : value;
    }

    function normalizeTablePosition(value, fallback = "bottom") {
      const normalized = String(value || "").trim().toLowerCase();
      if (["off", "top", "bottom"].includes(normalized)) return normalized;
      return fallback;
    }
