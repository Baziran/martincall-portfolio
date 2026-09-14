    window.mcDebugStep && window.mcDebugStep("main script started");
    const fmt = value => value === null || value === undefined ? "-" : Number(value).toFixed(2);
    const CORE_SIDE_PANEL_TABS = Object.freeze(["instruments", "indicators", "alerts", "go"]);

    function sidePanelTabs() {
      const extensions = Object.values(window.INDICATOR_REGISTRY_MANIFEST || {})
        .map(spec => spec?.extensions?.sidebar?.id)
        .filter(Boolean);
      return [...CORE_SIDE_PANEL_TABS, ...extensions];
    }

    function exactIdentityText(value) {
      return typeof value === "string" ? value : "";
    }

    const THEME_CSS_VARIABLES = [
      "--bg", "--panel", "--panel-2", "--text", "--muted", "--line",
      "--green", "--red", "--gold", "--blue", "--purple",
      "--chart-bg", "--grid-line", "--axis", "--crosshair", "--crosshair-muted",
      "--tooltip-bg", "--tooltip-text", "--level-high", "--level-low",
      "--ema20", "--ema233", "--vwap", "--drawing", "--drawing-muted", "--session-label", "--ny-range",
      "--canvas-up", "--canvas-down", "--canvas-gold", "--canvas-blue", "--canvas-purple",
      "--canvas-neutral", "--canvas-ink", "--canvas-axis", "--canvas-amber", "--canvas-cyan",
      "--canvas-label-light", "--canvas-label-dark",
    ];
    const mcThemeCache = {
      key: "",
      css: new Map(),
      colors: {},
      rgba: new Map(),
      parsed: new Map(),
      blended: new Map(),
      readable: new Map(),
      theme: new Map(),
    };
    const rgbaColorCache = mcThemeCache.rgba;

    function themeCacheKey() {
      const tone = document.body?.classList?.contains("theme-light") ? "light" : "dark";
      const shape = document.body?.classList?.contains("ui-tv-black")
        ? "tv-black"
        : document.body?.classList?.contains("ui-tv-flat") ? "tv-flat" : "";
      return `${tone}:${shape}`;
    }

    function clearDerivedThemeCaches() {
      mcThemeCache.rgba.clear();
      mcThemeCache.parsed.clear();
      mcThemeCache.blended.clear();
      mcThemeCache.readable.clear();
      mcThemeCache.theme.clear();
    }

    function refreshThemeColorCache(force = false) {
      const key = themeCacheKey();
      if (!force && mcThemeCache.key === key && mcThemeCache.css.size) return;
      const bodyStyle = getComputedStyle(document.body);
      const rootStyle = getComputedStyle(document.documentElement);
      mcThemeCache.key = key;
      mcThemeCache.css.clear();
      mcThemeCache.colors = {};
      for (const name of THEME_CSS_VARIABLES) {
        const value = (bodyStyle.getPropertyValue(name) || rootStyle.getPropertyValue(name)).trim();
        if (!value) continue;
        mcThemeCache.css.set(name, value);
        if (name.startsWith("--canvas-")) mcThemeCache.colors[name.slice("--canvas-".length)] = value;
      }
      clearDerivedThemeCaches();
    }

    function css(name) {
      const key = String(name || "");
      if (!key) return "";
      refreshThemeColorCache();
      if (mcThemeCache.css.has(key)) return mcThemeCache.css.get(key);
      const value = (
        getComputedStyle(document.body).getPropertyValue(key)
        || getComputedStyle(document.documentElement).getPropertyValue(key)
      ).trim();
      mcThemeCache.css.set(key, value);
      return value;
    }

    function themeColorTable() {
      refreshThemeColorCache();
      return mcThemeCache.colors;
    }
    function debugStep(phase, detail = "") {
      if (window.mcDebugStep) window.mcDebugStep(phase, detail);
    }
    const localTimeFormatter = new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
    const axisTimeFormatter = new Intl.DateTimeFormat(undefined, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    function saveBarsVisibleForCurrent() {
      const value = clamp(Math.round(Number(view.barsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      setServerSettingValue(barsVisibleStorageKey(state.instrumentId, state.timeframe), value);
    }

    let viewStatePersistTimer = 0;

    function persistViewState() {
      if (viewStatePersistTimer) {
        clearTimeout(viewStatePersistTimer);
        viewStatePersistTimer = 0;
      }
      saveBarsVisibleForCurrent();
      setServerSettingValue("aef:priceZoom", view.priceZoom);
      setServerSettingValue("aef:priceShift", view.priceShift);
      setServerSettingValue("aef:rightGapBars", view.rightGapBars);
      setServerSettingValue("aef:rightGapManual", view.rightGapManual ? "true" : "false");
      setServerSettingValue("aef:followLatest", view.followLatest);
    }

    function scheduleViewStatePersist(delayMs = 220) {
      if (viewStatePersistTimer) clearTimeout(viewStatePersistTimer);
      viewStatePersistTimer = setTimeout(persistViewState, Math.max(Number(delayMs) || 0, 0));
    }

    function sharedObjectsBroadcastKey(instrumentId = state.instrumentId, interval = state.timeframe, routeFingerprint = instrumentRouteFingerprint()) {
      return `aef:objectsUpdated:${JSON.stringify([
        exactIdentityText(instrumentId),
        exactIdentityText(routeFingerprint),
        String(interval || ""),
      ])}`;
    }

    function alertSemanticKey(alert) {
      return exactIdentityText(alert?.definition_identity || "");
    }

    const PRICE_ALERT_RUNTIME_FIELDS = Object.freeze([
      "armed",
      "fired",
      "cooldownUntil",
      "rearmedAt",
      "lastFiredAt",
      "lastFiredLevel",
      "touchDirection",
      "lastEventTs",
      "lastFiredEventTs",
      "telegramDeliveryStatus",
      "telegramNextRetryAt",
      "telegramRetryCount",
      "lastTelegramStatus",
      "lastTelegramOk",
    ]);

    function normalizePriceAlert(alert) {
      if (!alert || typeof alert !== "object" || Array.isArray(alert)) {
        throw new TypeError("PRICE_ALERT_ROW_INVALID: alert must be an object");
      }
      for (const field of [
        "level",
        "drawingAnchor",
        "lastAnchorBarSlot",
        "opacity",
        "deletable",
        "delete_icon",
        "label_handle",
        "interactive",
      ]) {
        if (Object.prototype.hasOwnProperty.call(alert, field)) {
          throw new TypeError(`PRICE_ALERT_FIELD_FORBIDDEN: ${field}`);
        }
      }
      const id = exactIdentityText(alert.id);
      const exactInstrumentId = exactIdentityText(alert.instrument_id);
      const routeFingerprint = exactIdentityText(alert.route_fingerprint);
      const provider = exactIdentityText(alert.provider);
      const providerContractId = exactIdentityText(alert.provider_contract_id);
      const symbol = exactIdentityText(alert.symbol);
      const timeframe = exactIdentityText(alert.timeframe);
      const kind = exactIdentityText(alert.kind);
      const direction = exactIdentityText(alert.direction);
      const definitionIdentity = alert.definition_identity === undefined
        ? ""
        : exactIdentityText(alert.definition_identity);
      if (!id || !exactInstrumentId || !routeFingerprint || !provider || !providerContractId || !symbol || !timeframe || !kind) {
        throw new TypeError("PRICE_ALERT_IDENTITY_INVALID: exact alert identity is required");
      }
      if (!["price", "ema233_touch", "vsa_fuel"].includes(kind)) {
        throw new TypeError(`PRICE_ALERT_FIELD_INVALID: ${id} kind`);
      }
      if (typeof alert.label !== "string") throw new TypeError(`PRICE_ALERT_FIELD_INVALID: ${id} label`);
      if (!["cross", "above", "below"].includes(direction)) throw new TypeError(`PRICE_ALERT_FIELD_INVALID: ${id} direction`);
      for (const field of ["price", "toleranceAtr", "tolerancePoints", "rearmedAt", "createdAt"]) {
        if (typeof alert[field] !== "number" || !Number.isFinite(alert[field])) {
          throw new TypeError(`PRICE_ALERT_FIELD_INVALID: ${id} ${field}`);
        }
      }
      if (typeof alert.enabled !== "boolean") throw new TypeError(`PRICE_ALERT_FIELD_INVALID: ${id} enabled`);
      if (!Number.isInteger(alert.rearmMinutes) || ![15, 30, 60].includes(alert.rearmMinutes)) {
        throw new TypeError(`PRICE_ALERT_FIELD_INVALID: ${id} rearmMinutes`);
      }
      return {
        ...alert,
        id,
        instrument_id: exactInstrumentId,
        route_fingerprint: routeFingerprint,
        provider,
        provider_contract_id: providerContractId,
        symbol,
        timeframe,
        price: alert.price,
        kind,
        label: alert.label,
        direction,
        definition_identity: definitionIdentity,
        toleranceAtr: alert.toleranceAtr,
        tolerancePoints: alert.tolerancePoints,
        enabled: alert.enabled,
        rearmedAt: alert.rearmedAt,
        rearmMinutes: alert.rearmMinutes,
        createdAt: alert.createdAt,
      };
    }

    function priceAlertClientPayload(alert) {
      const normalized = normalizePriceAlert(alert);
      const payload = {
        id: normalized.id,
        instrument_id: normalized.instrument_id,
        route_fingerprint: normalized.route_fingerprint,
        provider: normalized.provider,
        provider_contract_id: normalized.provider_contract_id,
        symbol: normalized.symbol,
        timeframe: normalized.timeframe,
        price: normalized.price,
        kind: normalized.kind,
        label: normalized.label,
        direction: normalized.direction,
        toleranceAtr: normalized.toleranceAtr,
        tolerancePoints: normalized.tolerancePoints,
        enabled: normalized.enabled,
        rearmedAt: normalized.rearmedAt,
        rearmMinutes: normalized.rearmMinutes,
        createdAt: normalized.createdAt,
      };
      return payload;
    }

    function dedupePriceAlerts(alerts) {
      const validated = [];
      const definitionIdentities = new Set();
      const alertIds = new Set();
      for (const raw of alerts || []) {
        const alert = normalizePriceAlert(raw);
        if (alertIds.has(alert.id)) throw new TypeError(`PRICE_ALERT_ID_DUPLICATE: ${alert.id}`);
        alertIds.add(alert.id);
        const key = alertSemanticKey(alert);
        if (key && definitionIdentities.has(key)) {
          throw new TypeError(`PRICE_ALERT_SEMANTIC_DUPLICATE: ${key}`);
        }
        if (key) definitionIdentities.add(key);
        validated.push(alert);
      }
      return validated;
    }

    function priceAlertMatchesScope(alert, instrumentId, interval) {
      const identity = exactIdentityText(instrumentId || "");
      const instrument = instrumentForId(identity);
      const routeFingerprint = instrument ? instrumentRouteFingerprint(instrument) : "";
      return exactIdentityText(alert?.instrument_id || "") === identity
        && exactIdentityText(alert?.route_fingerprint || "") === routeFingerprint
        && String(alert?.timeframe || "") === String(interval || "");
    }

    function priceAlertsForScope(alerts, instrumentId, interval) {
      return (alerts || []).filter(alert => priceAlertMatchesScope(alert, instrumentId, interval));
    }

    function replacePriceAlertsForScope(allAlerts, scopedAlerts, instrumentId, interval) {
      const scoped = dedupePriceAlerts(scopedAlerts);
      const others = (allAlerts || []).filter(alert => !priceAlertMatchesScope(alert, instrumentId, interval));
      return dedupePriceAlerts([...others, ...scoped]);
    }

    function normalizeDrawingPoint(point) {
      if (!point || typeof point !== "object" || Array.isArray(point)) {
        throw new TypeError("DRAWING_POINT_INVALID: point must be an object");
      }
      for (const field of ["bar_slot", "barSlot", "future", "slotAuthoritative", "optionTarget", "optionTargetAnchor"]) {
        if (Object.prototype.hasOwnProperty.call(point, field)) {
          throw new TypeError(`DRAWING_FIELD_FORBIDDEN: ${field}`);
        }
      }
      if (typeof point.price !== "number" || !Number.isFinite(point.price)) {
        throw new TypeError("DRAWING_FIELD_INVALID: price");
      }
      const hasTs = Object.prototype.hasOwnProperty.call(point, "ts");
      const hasAnchorTs = Object.prototype.hasOwnProperty.call(point, "anchorTs");
      const hasBarOffset = Object.prototype.hasOwnProperty.call(point, "barOffset");
      if (hasTs === (hasAnchorTs || hasBarOffset)) {
        throw new TypeError("DRAWING_FIELD_INVALID: anchor");
      }
      if (hasTs) {
        if (
          typeof point.ts !== "string"
          || !point.ts
          || !/(?:Z|[+-]\d{2}:\d{2})$/.test(point.ts)
          || !Number.isFinite(Date.parse(point.ts))
        ) {
          throw new TypeError("DRAWING_FIELD_INVALID: ts");
        }
        return { ts: point.ts, price: point.price };
      }
      if (
        !hasAnchorTs
        || !hasBarOffset
        || typeof point.anchorTs !== "string"
        || !point.anchorTs
        || !/(?:Z|[+-]\d{2}:\d{2})$/.test(point.anchorTs)
        || !Number.isFinite(Date.parse(point.anchorTs))
      ) {
        throw new TypeError("DRAWING_FIELD_INVALID: anchorTs");
      }
      if (!Number.isSafeInteger(point.barOffset) || point.barOffset <= 0) {
        throw new TypeError("DRAWING_FIELD_INVALID: barOffset");
      }
      return { anchorTs: point.anchorTs, barOffset: point.barOffset, price: point.price };
    }

    const RETIRED_DRAWING_ALERT_FIELDS = Object.freeze([
      "alertOnCross",
      "alertDeletedAt",
      "alertArmed",
      "alertFired",
      "alertCooldownUntil",
      "alertRearmedAt",
      "lastFiredAt",
      "lastFiredLevel",
      "lastTelegramStatus",
      "lastTelegramOk",
      "lastTelegramDetail",
      "telegramDeliveryStatus",
      "telegramPendingPayload",
      "telegramPendingSince",
      "telegramNextRetryAt",
      "telegramRetryCount",
      "telegramClaimedAt",
      "telegramLastAttemptAt",
      "telegramLastError",
    ]);

    function normalizedDrawingAnchorResolution(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const status = String(value.status || "");
      if (!['needs_history', 'resolved'].includes(status)) return null;
      const reasons = Array.isArray(value.reasons)
        ? [...new Set(value.reasons.map(item => String(item || "")).filter(Boolean))].sort()
        : [];
      return { status, reasons };
    }

    function drawingTimestampIdentity(value) {
      const parsed = Date.parse(typeof value === "string" ? value : "");
      return Number.isFinite(parsed) ? String(parsed) : "";
    }

    const normalizedDrawingAnchorProjectionValues = new WeakSet();
    const drawingProjectionIndexCache = new WeakMap();

    function normalizedDrawingAnchorProjection(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      if (normalizedDrawingAnchorProjectionValues.has(value)) return value;
      const instrumentId = exactIdentityText(value.instrumentId || "");
      const routeFingerprint = exactIdentityText(value.routeFingerprint || "");
      const timeframe = String(value.timeframe || "");
      const canonicalGeneration = Number(value.canonicalGeneration);
      const confirmedCount = Number(value.confirmedCount);
      const originTs = typeof value.originTs === "string" ? value.originTs : "";
      const confirmedThroughTs = typeof value.confirmedThroughTs === "string"
        ? value.confirmedThroughTs
        : "";
      const originKey = drawingTimestampIdentity(originTs);
      const confirmedThroughKey = drawingTimestampIdentity(confirmedThroughTs);
      if (
        !instrumentId
        || !routeFingerprint
        || !timeframe
        || !Number.isSafeInteger(canonicalGeneration)
        || canonicalGeneration < 0
        || !Number.isSafeInteger(confirmedCount)
        || confirmedCount <= 0
        || !originKey
        || !confirmedThroughKey
        || Number(confirmedThroughKey) < Number(originKey)
        || !Array.isArray(value.anchors)
      ) return null;
      const seen = new Set();
      const anchors = [];
      let previousTimestamp = -Infinity;
      let previousLogicalIndex = -1;
      for (const raw of value.anchors) {
        const ts = typeof raw?.ts === "string" ? raw.ts : "";
        const key = drawingTimestampIdentity(ts);
        const logicalIndex = Number(raw?.logicalIndex);
        if (
          !key
          || seen.has(key)
          || !Number.isSafeInteger(logicalIndex)
          || logicalIndex < 0
          || logicalIndex >= confirmedCount
          || Number(key) <= previousTimestamp
          || logicalIndex <= previousLogicalIndex
        ) return null;
        seen.add(key);
        anchors.push(Object.freeze({ ts, logicalIndex }));
        previousTimestamp = Number(key);
        previousLogicalIndex = logicalIndex;
      }
      if (
        !anchors.length
        || drawingTimestampIdentity(anchors[0].ts) !== originKey
        || anchors[0].logicalIndex !== 0
        || previousTimestamp > Number(confirmedThroughKey)
      ) return null;
      const normalized = Object.freeze({
        instrumentId,
        routeFingerprint,
        timeframe,
        canonicalGeneration,
        originTs,
        confirmedThroughTs,
        confirmedCount,
        anchors: Object.freeze(anchors),
      });
      normalizedDrawingAnchorProjectionValues.add(normalized);
      return normalized;
    }

    function normalizeDrawingObjects(drawings) {
      if (!Array.isArray(drawings)) throw new TypeError("DRAWING_ROWS_INVALID: drawings must be an array");
      const pointCounts = { line: 2, zone: 2, fib: 2, channel: 2, ellipse: 3, text: 1 };
      const drawingIds = new Set();
      let drawingAnchorPointCount = 0;
      return drawings.map(rawItem => {
        if (!rawItem || typeof rawItem !== "object" || Array.isArray(rawItem)) {
          throw new TypeError("DRAWING_ROW_INVALID: drawing must be an object");
        }
        const item = { ...rawItem };
        const incomingResolution = normalizedDrawingAnchorResolution(item.anchorResolution);
        const incomingProjection = normalizedDrawingAnchorProjection(item.anchorProjection);
        for (const field of RETIRED_DRAWING_ALERT_FIELDS) delete item[field];
        for (const field of ["anchorProjection", "geometryIntegrity", "channelIntegrity", "channelCompromised"]) delete item[field];
        if (incomingResolution) item.anchorResolution = incomingResolution;
        else delete item.anchorResolution;
        if (incomingProjection) item.anchorProjection = incomingProjection;
        for (const field of [
          "offset",
          "anchorCoverage",
          "offset_point",
          "opacity",
          "deletable",
          "delete_icon",
          "label_handle",
          "interactive",
        ]) {
          if (Object.prototype.hasOwnProperty.call(item, field)) {
            throw new TypeError(`DRAWING_FIELD_FORBIDDEN: ${field}`);
          }
        }
        if (typeof item.id !== "string" || !item.id) throw new TypeError("DRAWING_FIELD_INVALID: id");
        if (drawingIds.has(item.id)) throw new TypeError(`DRAWING_ID_DUPLICATE: ${item.id}`);
        drawingIds.add(item.id);
        if (!Object.prototype.hasOwnProperty.call(pointCounts, item.type) && item.type !== "path") {
          throw new TypeError(`DRAWING_FIELD_INVALID: ${item.id} type`);
        }
        if (Object.prototype.hasOwnProperty.call(item, "lineVariant")) {
          if (item.type !== "line") {
            throw new TypeError(`DRAWING_FIELD_FORBIDDEN: ${item.id} lineVariant`);
          }
          if (item.lineVariant !== "ruler") {
            throw new TypeError(`DRAWING_FIELD_INVALID: ${item.id} lineVariant`);
          }
          if (item.extendRight !== false) {
            throw new TypeError(`DRAWING_FIELD_INVALID: ${item.id} extendRight`);
          }
        }
        if (Object.prototype.hasOwnProperty.call(item, "extendRight")) {
          if (typeof item.extendRight !== "boolean") {
            throw new TypeError(`DRAWING_FIELD_INVALID: ${item.id} extendRight`);
          }
          if (!["line", "zone", "channel", "fib"].includes(item.type)) {
            throw new TypeError(`DRAWING_FIELD_FORBIDDEN: ${item.id} extendRight`);
          }
        }
        const required = pointCounts[item.type];
        if (
          !Array.isArray(item.points)
          || (required !== undefined && item.points.length !== required)
          || (item.type === "path" && item.points.length < 2)
        ) {
          throw new TypeError(`DRAWING_FIELD_INVALID: ${item.id} points`);
        }
        item.points = item.points.map(normalizeDrawingPoint);
        if (item.type === "channel") {
          if (!item.offsetPoint || typeof item.offsetPoint !== "object" || Array.isArray(item.offsetPoint)) {
            throw new TypeError(`DRAWING_FIELD_INVALID: ${item.id} offsetPoint`);
          }
          item.offsetPoint = normalizeDrawingPoint(item.offsetPoint);
        } else if (Object.prototype.hasOwnProperty.call(item, "offsetPoint")) {
          throw new TypeError(`DRAWING_FIELD_FORBIDDEN: ${item.id} offsetPoint`);
        }
        drawingAnchorPointCount += item.points.length + (item.type === "channel" ? 1 : 0);
        if (drawingAnchorPointCount > DRAWING_MAX_ANCHOR_POINTS) {
          throw new TypeError(
            `DRAWING_ANCHOR_LIMIT_EXCEEDED: ${drawingAnchorPointCount} > ${DRAWING_MAX_ANCHOR_POINTS}`,
          );
        }
        return item;
      });
    }

    function durableDrawingObjects(drawings) {
      return normalizeDrawingObjects(drawings).map(item => {
        const durable = { ...item };
        delete durable.anchorResolution;
        delete durable.anchorProjection;
        delete durable.geometryIntegrity;
        delete durable.channelIntegrity;
        delete durable.channelCompromised;
        return durable;
      });
    }

    function drawingProjectionIndex(projection) {
      const normalized = normalizedDrawingAnchorProjection(projection);
      if (!normalized) return null;
      const cached = drawingProjectionIndexCache.get(normalized);
      if (cached) return cached;
      const byTimestamp = new Map(
        normalized.anchors.map(item => [drawingTimestampIdentity(item.ts), item.logicalIndex]),
      );
      const indexed = Object.freeze({ ...normalized, byTimestamp });
      drawingProjectionIndexCache.set(normalized, indexed);
      return indexed;
    }

    function drawingProjectionSnapshotScope(snapshot) {
      const meta = snapshot?.meta || {};
      const hasMetaInstrumentId = Boolean(meta.instrument_id);
      const hasMetaRouteFingerprint = Boolean(meta.route_fingerprint);
      const metaInstrumentId = exactIdentityText(meta.instrument_id || "");
      const metaRouteFingerprint = exactIdentityText(meta.route_fingerprint || "");
      const metaTimeframe = String(meta.timeframe || "");
      const currentRouteFingerprint = !hasMetaRouteFingerprint
        && typeof instrumentRouteFingerprint === "function"
        ? instrumentRouteFingerprint()
        : "";
      return {
        instrumentId: hasMetaInstrumentId
          ? metaInstrumentId
          : exactIdentityText(state.instrumentId || ""),
        routeFingerprint: hasMetaRouteFingerprint
          ? metaRouteFingerprint
          : exactIdentityText(currentRouteFingerprint),
        timeframe: metaTimeframe || String(state.timeframe || ""),
        canonicalGeneration: Number(meta.chart_canonical_revision),
      };
    }

    function drawingProjectionSnapshotCacheKey(snapshot) {
      const scope = drawingProjectionSnapshotScope(snapshot);
      return JSON.stringify([
        scope.instrumentId,
        scope.routeFingerprint,
        scope.timeframe,
        Number.isSafeInteger(scope.canonicalGeneration) ? scope.canonicalGeneration : null,
      ]);
    }

    function drawingProjectionMatchesSnapshotScope(projection, snapshot) {
      const scope = drawingProjectionSnapshotScope(snapshot);
      return Boolean(
        projection
        && projection.instrumentId === scope.instrumentId
        && projection.routeFingerprint === scope.routeFingerprint
        && projection.timeframe === scope.timeframe
      );
    }

    function drawingProjectionMatchesSnapshot(projection, snapshot) {
      const scope = drawingProjectionSnapshotScope(snapshot);
      return Boolean(
        drawingProjectionMatchesSnapshotScope(projection, snapshot)
        && projection.canonicalGeneration === scope.canonicalGeneration
      );
    }

    function drawingProjectionSnapshotReadiness(snapshot, expectedScope = null) {
      const meta = snapshot?.meta;
      if (!meta || !Array.isArray(snapshot?.bars)) {
        return { ready: false, reason: "snapshot_unavailable" };
      }
      const scope = drawingProjectionSnapshotScope(snapshot);
      const hasExplicitExpectedScope = Boolean(
        expectedScope && typeof expectedScope === "object"
      );
      const currentRouteFingerprint = !hasExplicitExpectedScope
        && typeof instrumentRouteFingerprint === "function"
        ? instrumentRouteFingerprint()
        : "";
      const expected = hasExplicitExpectedScope
        ? {
            instrumentId: exactIdentityText(expectedScope.instrumentId || ""),
            routeFingerprint: exactIdentityText(expectedScope.routeFingerprint || ""),
            timeframe: String(expectedScope.timeframe || ""),
          }
        : {
            instrumentId: exactIdentityText(state.instrumentId || ""),
            routeFingerprint: exactIdentityText(currentRouteFingerprint),
            timeframe: String(state.timeframe || ""),
          };
      if (
        (hasExplicitExpectedScope && (
          !expected.instrumentId
          || !expected.routeFingerprint
          || !expected.timeframe
        ))
        || scope.instrumentId !== expected.instrumentId
        || scope.routeFingerprint !== expected.routeFingerprint
        || scope.timeframe !== expected.timeframe
      ) {
        return { ready: false, reason: "snapshot_scope_mismatch" };
      }
      if (!Number.isSafeInteger(scope.canonicalGeneration) || scope.canonicalGeneration < 0) {
        return { ready: false, reason: "snapshot_generation_pending" };
      }
      return { ready: true, reason: "" };
    }

    function drawingProjectionSnapshotOffset(projection, confirmedBars, confirmedIndexByTimestamp = null) {
      if (!projection || !Array.isArray(confirmedBars) || !confirmedBars.length) return null;
      const localByTimestamp = confirmedIndexByTimestamp instanceof Map
        ? confirmedIndexByTimestamp
        : new Map(
            confirmedBars.map((bar, index) => [drawingTimestampIdentity(bar?.ts), index]),
          );
      const references = [
        [drawingTimestampIdentity(projection.confirmedThroughTs), projection.confirmedCount - 1],
        ...projection.anchors.map(item => [drawingTimestampIdentity(item.ts), item.logicalIndex]),
      ];
      const offsets = references
        .filter(([key]) => key && localByTimestamp.has(key))
        .map(([key, logicalIndex]) => logicalIndex - localByTimestamp.get(key));
      if (!offsets.length || offsets.some(value => value !== offsets[0])) return null;
      return offsets[0];
    }

    function drawingAnchorValidationPending(object, snapshot = state.snapshot) {
      const resolution = normalizedDrawingAnchorResolution(object?.anchorResolution);
      if (resolution?.status === "needs_history") return true;
      const projection = drawingProjectionIndex(object?.anchorProjection);
      return Boolean(projection && !drawingProjectionMatchesSnapshot(projection, snapshot));
    }

    function resolveDrawingAnchorsAgainstSnapshot(drawings, snapshot = state.snapshot, options = {}) {
      if (!Array.isArray(drawings)) return false;
      const expectedScope = options?.expectedScope && typeof options.expectedScope === "object"
        ? options.expectedScope
        : null;
      const projectionRefreshOwner = typeof options?.onProjectionRefreshRequired === "function"
        ? options.onProjectionRefreshRequired
        : null;
      const snapshotReadiness = drawingProjectionSnapshotReadiness(snapshot, expectedScope);
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      const confirmedBars = [];
      const confirmedIndexByTimestamp = new Map();
      for (const bar of bars) {
        const key = drawingTimestampIdentity(bar?.ts);
        const confirmedBar = barIsConfirmedClosed(bar) && bar?.authoritative !== false;
        if (key && confirmedBar) {
          confirmedIndexByTimestamp.set(key, confirmedBars.length);
          confirmedBars.push(bar);
        }
      }
      let changed = false;
      let projectionRefreshRequired = false;
      for (const object of drawings) {
        if (!object || typeof object !== "object") continue;
        const requiredPoints = Array.isArray(object.points)
          ? object.points.filter(point => point && typeof point === "object")
          : [];
        if (object.type === "channel" && object.offsetPoint && typeof object.offsetPoint === "object") {
          requiredPoints.push(object.offsetPoint);
        }
        const projection = drawingProjectionIndex(object.anchorProjection);
        const projectionScopeMatches = drawingProjectionMatchesSnapshotScope(projection, snapshot);
        const projectionMatches = drawingProjectionMatchesSnapshot(projection, snapshot);
        const projectionOffset = projectionScopeMatches
          ? drawingProjectionSnapshotOffset(
              projection,
              confirmedBars,
              confirmedIndexByTimestamp,
            )
          : null;
        const previousGeometry = object.geometryIntegrity && typeof object.geometryIntegrity === "object"
          ? object.geometryIntegrity
          : null;
        const geometryReasons = [];
        let geometryStatus = "valid";
        let validationInconclusive = false;
        const serverResolution = normalizedDrawingAnchorResolution(object.anchorResolution);
        if (serverResolution?.status === "needs_history") {
          validationInconclusive = true;
          projectionRefreshRequired = true;
        }
        if (
          snapshotReadiness.ready
          && projection
          && (!projectionScopeMatches || !projectionMatches)
        ) {
          projectionRefreshRequired = true;
        }
        for (const point of requiredPoints) {
          if (!point || typeof point !== "object") {
            geometryStatus = "invalid";
            geometryReasons.push("missing_anchor");
            continue;
          }
          const anchorTs = typeof point.ts === "string" ? point.ts : point.anchorTs;
          const key = drawingTimestampIdentity(anchorTs);
          if (!key) {
            geometryStatus = "invalid";
            geometryReasons.push("invalid_anchor_timestamp");
          } else if (confirmedIndexByTimestamp.has(key)) {
            continue;
          } else if (!snapshotReadiness.ready) {
            validationInconclusive = true;
            continue;
          } else if (!projection) {
            validationInconclusive = true;
            projectionRefreshRequired = true;
          } else if (!projectionScopeMatches) {
            validationInconclusive = true;
            projectionRefreshRequired = true;
          } else if (projectionOffset === null) {
            validationInconclusive = true;
            projectionRefreshRequired = true;
          } else if (!projection.byTimestamp.has(key)) {
            geometryStatus = "invalid";
            geometryReasons.push("anchor_absent_from_projection");
          }
        }
        if (
          geometryStatus === "valid"
          && validationInconclusive
          && previousGeometry?.status === "invalid"
        ) {
          geometryStatus = "invalid";
          geometryReasons.push(
            ...(Array.isArray(previousGeometry.reasons) ? previousGeometry.reasons : []),
          );
        }
        const geometry = {
          status: geometryStatus,
          reasons: [...new Set(geometryReasons.filter(Boolean))].sort(),
        };
        if (JSON.stringify(object.geometryIntegrity || null) !== JSON.stringify(geometry)) {
          object.geometryIntegrity = geometry;
          changed = true;
        }
        if (object.type === "channel") {
          if (Object.prototype.hasOwnProperty.call(object, "channelIntegrity")) {
            delete object.channelIntegrity;
            changed = true;
          }
          if (Object.prototype.hasOwnProperty.call(object, "channelCompromised")) {
            delete object.channelCompromised;
            changed = true;
          }
        }
      }
      if (projectionRefreshRequired && projectionRefreshOwner) {
        projectionRefreshOwner({ drawings, snapshot, expectedScope });
      } else if (
        projectionRefreshRequired
        && !expectedScope
        && typeof scheduleDrawingProjectionRefresh === "function"
      ) {
        scheduleDrawingProjectionRefresh();
      } else if (
        snapshotReadiness.ready
        && !expectedScope
        && drawings === state.drawing.objects
        && typeof completeDrawingProjectionRefresh === "function"
      ) {
        completeDrawingProjectionRefresh();
      }
      return changed;
    }

    const STRATEGY_MODE_CODES = Object.freeze(["balanced", "mean_reversion", "breakout"]);
    const storedStrategyMode = storedInstrumentIndicatorString(
      "strategyMode",
      "balanced",
      initialInstrumentId,
    );

    const state = {
      workspaceSlot: activeWorkspaceSlot,
      instrumentId: initialInstrumentId,
      instrumentSelectionStatus: {
        state: initialInstrumentId ? "pending" : "empty",
        code: initialInstrumentId ? "INSTRUMENT_SELECTION_PENDING" : "WATCHLIST_EMPTY",
        instrument_id: initialInstrumentId,
        message: initialInstrumentId ? "Resolving saved instrument selection." : "Watchlist is empty.",
      },
      symbol: initialSymbol,
      strategyMode: STRATEGY_MODE_CODES.includes(storedStrategyMode) ? storedStrategyMode : "balanced",
      dataSource: initialDataSource,
      providers: [],
      instruments: [],
      watchlistVersion: null,
      screener: [],
      screenerByIdentity: new Map(),
      watchlistPresentationByInstrument: new Map(),
      watchlistTrendsByIdentity: new Map(),
      economicCalendar: {
        schema: "",
        status: "idle",
        events: [],
        sources: [],
        generatedAt: "",
        loading: false,
        started: false,
        sequence: 0,
        abort: null,
        refreshTimer: null,
      },
      screenerStatus: { ok: true, message: "" },
      storageStatus: { ok: true, message: "" },
      snapshot: null,
      lastPrice: null,
      watchlistEditMode: false,
      liveQuote: null,
      liveBarPreviewCache: {},
      timeframe: initialTimeframe,
      range: workspacePopout && workspaceUrlValue("range")
        ? initialRangeFor(initialTimeframe, workspaceUrlValue("range"), { allowExpanded: true })
        : storedRangeForInstrument(initialInstrumentId, initialTimeframe),
      clientId: `mc-${createBrowserUuidV4()}`,
      requests: {
        market: {
          loading: false,
          pending: false,
          pendingOptions: null,
          key: "",
          abort: null,
        },
        analysis: {
          loading: false,
          key: "",
          abort: null,
          pollTimer: null,
          pollKey: "",
          pollAttempt: 0,
          leaseKey: "",
          leaseInstrumentId: "",
          leaseRouteFingerprint: "",
          leaseTimer: null,
          leaseSequence: 0,
          leaseActiveSequence: 0,
        },
        screener: { loading: false, key: "" },
        watchlistTrends: { loading: false, key: "" },
        history: {
          loading: false,
          exhausted: false,
          queued: false,
          intentActive: false,
          abort: null,
          sequence: 0,
        },
      },
      transport: {
        chart: {
          socket: null,
          key: "",
          retryTimer: null,
          staleTimer: null,
          openedAt: 0,
          lastMessageAt: 0,
          lastBarsAt: 0,
          hasLiveBar: false,
          epoch: 0,
          generation: 0,
          sequence: 0,
          expectedLiveSlot: "",
          expectedLiveClose: "",
        },
        quote: {
          socket: null,
          key: "",
          retryTimer: null,
          openedAt: 0,
          lastMessageAt: 0,
          lastStateAt: 0,
          candleSequence: 0,
          stateSequence: 0,
          stateReady: false,
          alertRuntimeReady: false,
          alertRuntimeWarning: "",
          degradedWarning: "",
          cacheEpoch: "",
          cacheGeneration: 0,
          epoch: 0,
          mode: "idle",
          errorCode: "",
          errorMessage: "",
          commitPerf: null,
          paintPerf: null,
          gatewayPaintAtMs: 0,
        },
        browserCapture: {
          socket: null,
          key: "",
          retryTimer: null,
          ready: false,
          openedAt: 0,
          lastStateAt: 0,
          epoch: 0,
        },
      },
      pendingLiveCandlePaintAt: "",
      chartBarIndex: new Map(),
      chartBarIndexBars: null,
      chartBarIndexLength: 0,
      chartBarQueue: {
        key: "",
        barsByTs: new Map(),
        anonymous: [],
        flushScheduled: false,
        suppressAnalysis: true,
        chartDataQuality: null,
        historyCoverage: null,
        gapRepair: null,
        futureAxis: null,
        futureAxisCanonicalRevision: 0,
        chartQualityRevision: 0,
        canonicalRevision: 0,
        expectedLiveSlot: undefined,
        expectedLiveClose: undefined,
      },
      lastMarketLoadAt: 0,
      lastVisibilityHiddenAt: 0,
      lastVisibilityReloadAt: 0,
      lastLiveCandleRepairAt: 0,
      lastPassiveChartRenderAt: 0,
      lastIndicatorAnalysisRefreshAt: 0,
      analysisMissingRefreshKey: "",
      analysisMissingRecoveryScope: "",
      analysisMissingRecoveryAttempt: 0,
      indicatorAnalysisRefreshBar: "",
      indicatorAnalysisRefreshTimer: null,
      lastIndicatorAnalysisMergedVersion: "",
      lastLiveQuoteAnalysisConfirmedBar: "",
      lastQuoteAt: 0,
      lastHeaderRenderAt: 0,
      lastHeaderText: "",
      lastHeaderTitle: "",
      lastHeaderStableKey: "",
      lastDocumentTitleKey: "",
      notices: Object.create(null),
      noticeSequence: 0,
      systemHealth: null,
      serverSleeping: false,
      settings: {
        theme: normalizeThemeSetting(workspaceValue("theme"), "dark"),
        uiShape: normalizeUiShape(serverSettingValue("aef:uiShape")),
        showHorizontalGrid: storedBool("aef:showHorizontalGrid", true),
        showVerticalGrid: storedBool("aef:showVerticalGrid", true),
        showCandleGrid: storedBool("aef:showCandleGrid", false),
        showBidAskOnCandle: storedBool("aef:showBidAskOnCandle", false),
        economicCalendarEnabled: storedBool("aef:economicCalendar", true),
        healthVisualizer: loadHealthVisualizerSetting(),
        motionPreference: normalizeMotionPreference(serverSettingValue("aef:motionPreference")),
        sessionPriceOpacity: 1,
        sessionVolumeOpacity: 3,
        signalsRange: normalizeSignalsRange("2d"),
        signalDensity: normalizeSignalDensity("focus"),
        chartViewMode: normalizeChartViewMode(serverSettingValue("aef:chartViewMode")),
        indicatorLabelStyle: normalizeIndicatorLabelStyle("text"),
        paperMinRr: 1.25,
        paperAutoTrading: false,
        paperEdgeGate: storedBool("aef:paperEdgeGate", true),
        paperTelegramFeed: storedBool("aef:paperTelegramFeed", false),
        optionCaps: {},
        optionTargetDte: "1dte",
        gexSchedulerEnabled: false,
        gexSchedulerInstrumentIds: [],
        gexFocusMode: storedWorkspaceBool("gexFocusMode", false),
        viewPreset: normalizeViewPreset(workspaceValue("viewPreset")),
        tradeSetupRuntimeLayout: "full",
        indicatorMode: activeIndicatorMode,
        gexMode: activeIndicatorMode === "gex",
        ibkrPort: 7497,
        open: false,
      },
      sideTab: "instruments",
      crosshair: {
        visible: false,
        source: null,
        x: 0,
        y: 0,
        index: null,
        ts: null,
        price: null,
        snapKind: null,
      },
      priceScale: {
        percentHover: false,
      },
      contextMenu: { open: false, x: 0, y: 0, bar: null, price: null },
      alerts: {
        rules: [],
        drag: null,
        selectedId: null,
        toastTimer: null,
        syncEpoch: 0,
        runtimeByScope: new Map(),
      },
      tooltipHits: [],
      marketContext: {},
      gexContext: null,
      gexContexts: {},
      loadingGex: false,
      loadingGexKeys: {},
      gexInitialAcquireKeys: {},
      gexReadModelHandoffTimers: {},
      gexManualRefresh: {
        active: false,
        status: "idle",
        message: "",
        requestKey: "",
        startedAt: 0,
        finishedAt: 0,
        capturedAt: "",
        pollTimer: null,
      },
      gexLive: {
        lifecycleGeneration: 0,
        motionTimer: null,
        motionEvents: {},
        pendingHistory: null,
      },
      optionBoard: {
        open: false,
        surface: "",
        socket: null,
        routeKey: "",
        expiryMode: "hybrid",
        lifecycleGeneration: 0,
        retryTimer: null,
        staleTimer: null,
        status: "closed",
        message: "",
        snapshot: null,
        revision: 0,
        lastMessageAt: 0,
        addingToken: "",
      },
      workspaceDock: {
        open: false,
        surface: "",
      },
      lastGexContextPollAt: 0,
      lastGexRecalcCapturedAt: "",
      pendingGexRecalcCapturedAt: "",
      optionTargets: {
        items: [],
        selectedId: null,
        draggingId: null,
        pendingMutationKeys: {},
        renderTimer: null,
        deletedIds: {},
      },
      fastIndicators: {
        revisionsByScope: {},
        aggregateByScope: {},
        optionReversalByTargetIdentity: {},
        primaryOptionReversalByScope: {},
      },
      urgentRiskMessages: [],
      urgentRiskLastKey: "",
      urgentRiskDismissed: {},
      urgentRiskSnoozedUntil: {},
      urgentRiskPresentationLoaded: false,
      urgentRiskSnoozeWakeAt: 0,
      urgentRiskSnoozeTimer: null,
      indicators: {
        globalDefaults: {
          preset: storedGlobalIndicatorString("globalDefaultPreset", "balanced"),
          atrLen: storedGlobalIndicatorNumber("globalAtrLen", 14),
          rvolLen: storedGlobalIndicatorNumber("globalRvolLen", 30),
          emaPullback: storedGlobalIndicatorNumberInRange("globalEmaPullback", 20, 5, 300),
          emaFast: storedGlobalIndicatorNumberInRange("globalEmaFast", 21, 5, 300),
          emaSlow: storedGlobalIndicatorNumberInRange("globalEmaSlow", 55, 10, 400),
          emaMagnet: storedGlobalIndicatorNumberInRange("globalEmaMagnet", 233, 50, 1000),
          scorePre: storedGlobalIndicatorNumber("globalScorePre", 42),
          scoreWatch: storedGlobalIndicatorNumber("globalScoreWatch", 58),
          scoreArm: storedGlobalIndicatorNumber("globalScoreArm", 70),
          scoreGo: storedGlobalIndicatorNumber("globalScoreGo", 78),
          rvolLow: storedGlobalIndicatorNumber("globalRvolLow", 0.85),
          rvolElevated: storedGlobalIndicatorNumber("globalRvolElevated", 1.10),
          rvolHigh: storedGlobalIndicatorNumber("globalRvolHigh", 1.20),
          rvolClimax: storedGlobalIndicatorNumber("globalRvolClimax", 1.60),
        },
        ema233: {
          allEnabled: storedGlobalIndicatorBool("emaAllEnabled", true),
          enabled: storedGlobalIndicatorBool("ema233Enabled", true),
          ema20Enabled: storedGlobalIndicatorBool("ema20Enabled", true),
          ema50Enabled: storedGlobalIndicatorBool("ema50Enabled", false),
          ema20Color: storedGlobalIndicatorString("ema20Color", css("--ema20") || "#facc15"),
          ema50Color: storedGlobalIndicatorString("ema50Color", ""),
          color: storedGlobalIndicatorString("ema233Color", css("--ema233") || "#38bdf8"),
          style: storedGlobalIndicatorString("ema233Style", "solid"),
          width: storedGlobalIndicatorNumber("ema233Width", 2),
          alertRearmMinutes: storedGlobalIndicatorNumber("ema233AlertRearmMinutes", 60),
        },
        vwap: {
          enabled: storedGlobalIndicatorBool("vwapEnabled", true),
          style: storedGlobalIndicatorString("vwapStyle", "dashed"),
          width: storedGlobalIndicatorNumber("vwapWidth", 1),
          color: storedGlobalIndicatorString("vwapColor", css("--vwap") || "#22c55e"),
          bands: storedGlobalIndicatorBool("vwapBands", true),
          bandCount: storedGlobalIndicatorNumber("vwapBandCount", 2),
          bandColor: storedGlobalIndicatorString("vwapBandColor", ""),
        },
        vsaVolume: {
          visible: storedGlobalIndicatorBool("vsaVolumeVisible", true),
          avg: storedGlobalIndicatorBool("vsaVolumeAvg", true),
          labels: storedGlobalIndicatorBool("vsaVolumeLabels", true),
          priceMarks: storedGlobalIndicatorBool("vsaVolumePriceMarks", true),
          candleColors: storedGlobalIndicatorBool("vsaVolumeCandleColors", true),
          volumeColors: storedGlobalIndicatorBool("vsaVolumeVolumeColors", false),
          renderHours: storedGlobalIndicatorNumber("vsaVolumeRenderHours", 6),
        },
        gexContext: {
          enabled: storedInstrumentIndicatorBool("gexContextEnabled", false, initialInstrumentId),
          mode: storedInstrumentIndicatorString("gexContextMode", "request", initialInstrumentId),
          zones: storedInstrumentIndicatorBool("gexContextZones", true, initialInstrumentId),
          zoneStyle: storedInstrumentIndicatorString(
            "gexContextZoneStyle",
            storedInstrumentIndicatorBool("gexContextZones", true, initialInstrumentId) ? "lines" : "off",
            initialInstrumentId,
          ),
          profile: storedInstrumentIndicatorBool("gexContextProfile", true, initialInstrumentId),
          profileStyle: storedInstrumentIndicatorString("gexContextProfileStyle", "mini", initialInstrumentId),
          historyView: storedGexHistoryView(initialInstrumentId),
          displayLevels: (() => {
            const stored = storedInstrumentIndicatorNumber(
              "gexContextDisplayLevels",
              GEX_DEFAULT_DISPLAY_LEVEL_COUNT,
              initialInstrumentId,
            );
            return GEX_DISPLAY_LEVEL_COUNTS.includes(stored)
              ? stored
              : GEX_DEFAULT_DISPLAY_LEVEL_COUNT;
          })(),
          dynamicsVisible: storedInstrumentIndicatorBool(
            "gexDynamicsVisible",
            false,
            initialInstrumentId,
          ),
        },
        optionTargets: {
          enabled: storedInstrumentIndicatorBool("optionTargetsEnabled", true, initialInstrumentId),
          fastStatus: storedInstrumentIndicatorBool(
            "optionTargetsFastStatus",
            true,
            initialInstrumentId,
          ),
          pulse: storedInstrumentIndicatorBool("optionTargetsPulse", true, initialInstrumentId),
        },
        tradePlan: {
          enabled: false,
          width: 1,
        },
        nyRange: {
          enabled: storedGlobalIndicatorBool("nyRangeEnabled", true),
          opacity: storedGlobalIndicatorNumber("nyRangeOpacity", 14),
          style: storedGlobalIndicatorString("nyRangeStyle", "dotted"),
          priceLines: storedGlobalIndicatorBool("nyRangeLines", true),
          prevDayLevels: storedGlobalIndicatorBool("prevDayLevelsEnabled", false),
        },
      },
      drawing: {
        tool: "cursor",
        lineMode: "line",
        cursorMode: normalizeCursorMode(serverSettingValue("aef:cursorMode")),
        magnet: storedBool("aef:drawingMagnet", true),
        hidden: storedBool("aef:drawingHidden", false),
        objects: [],
        draft: null,
        hoverPoint: null,
        selectedId: null,
        drag: null,
        deletedIds: {},
        localEditUntil: 0,
        persistedCount: 0,
        managerOpen: false,
        managerType: "all",
      },
      tooltip: {
        price: [],
        volume: [],
      },
      paperStats: null,
      paperStatsKey: "",
      paperStatsBaseline: null,
      paperStatsBaselineKey: "",
      paperOrders: [],
      paperOrdersKey: "",
      paperOrdersPromise: null,
      paperOrderActionHits: [],
      paperTrading: {
        expanded: true,
        side: null,
        orderType: "limit",
        qty: 1,
        useStopLoss: true,
        stopPoints: 8,
        useTarget: true,
        targetPoints: 16,
        dragOrder: null,
        armed: false,
        submitting: false,
        status: "Select BUY or SELL, then click a price on the chart.",
      },
      loadingPaperStats: false,
      paperStatsPromise: null,
      paperStatsForce: false,
      lastPaperStatsLoadAt: 0,
    };

    const workspaceDockSurfaceRegistry = new Map();

    function currentWorkspaceDockState() {
      state.workspaceDock = state.workspaceDock || { open: false, surface: "" };
      return state.workspaceDock;
    }

    function registerWorkspaceDockSurface(surface, lifecycle = {}) {
      const key = String(surface || "").trim();
      if (!key || !lifecycle || typeof lifecycle !== "object") {
        throw new Error(`Invalid workspace dock surface: ${key || "missing"}`);
      }
      if (workspaceDockSurfaceRegistry.has(key)) {
        throw new Error(`Duplicate workspace dock surface: ${key}`);
      }
      workspaceDockSurfaceRegistry.set(key, lifecycle);
    }

    function workspaceDockSurfaceIsActive(surface) {
      const dock = currentWorkspaceDockState();
      return dock.open && dock.surface === String(surface || "");
    }

    function renderWorkspaceDockShell() {
      const dock = currentWorkspaceDockState();
      const active = dock.open ? dock.surface : "";
      const root = document.getElementById("workspace-dock");
      root?.classList.toggle("collapsed", !active);
      document.body.classList.toggle("workspace-dock-open", Boolean(active));
      document.body.classList.toggle("option-drive-open", active === "option-drive");
      document.querySelectorAll("[data-workspace-dock-surface]").forEach(button => {
        const selected = button.dataset.workspaceDockSurface === active;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-expanded", selected ? "true" : "false");
        button.setAttribute("aria-selected", selected ? "true" : "false");
      });
      document.querySelectorAll("[data-workspace-dock-panel]").forEach(panel => {
        const selected = panel.dataset.workspaceDockPanel === active;
        panel.hidden = !selected;
        panel.setAttribute("aria-hidden", selected ? "false" : "true");
      });
      if (typeof refreshWorkspaceDockLayout === "function") refreshWorkspaceDockLayout();
    }

    function closeWorkspaceDockSurface(surface = "", options = {}) {
      const dock = currentWorkspaceDockState();
      const active = dock.open ? dock.surface : "";
      const requested = String(surface || "").trim();
      if (!active || (requested && requested !== active)) return false;
      const lifecycle = workspaceDockSurfaceRegistry.get(active);
      dock.open = false;
      dock.surface = "";
      renderWorkspaceDockShell();
      if (typeof lifecycle?.deactivate === "function") lifecycle.deactivate(options);
      return true;
    }

    function openWorkspaceDockSurface(surface, options = {}) {
      const key = String(surface || "").trim();
      const lifecycle = workspaceDockSurfaceRegistry.get(key);
      if (!lifecycle) throw new Error(`Unknown workspace dock surface: ${key || "missing"}`);
      const dock = currentWorkspaceDockState();
      if (dock.open && dock.surface === key) {
        renderWorkspaceDockShell();
        if (typeof lifecycle.refresh === "function") lifecycle.refresh(options);
        return true;
      }
      if (dock.open && dock.surface) {
        closeWorkspaceDockSurface(dock.surface, {
          reason: options.reason || `Switched to ${key}`,
        });
      }
      dock.open = true;
      dock.surface = key;
      renderWorkspaceDockShell();
      if (typeof lifecycle.activate === "function") lifecycle.activate(options);
      return true;
    }

    function toggleWorkspaceDockSurface(surface, options = {}) {
      if (workspaceDockSurfaceIsActive(surface)) {
        return closeWorkspaceDockSurface(surface, options);
      }
      return openWorkspaceDockSurface(surface, options);
    }

    function setChartObjectSelection(kind, id) {
      const selectedKind = ["drawing", "alert", "option-target"].includes(kind) ? kind : "";
      const selectedId = String(id || "") || null;
      state.drawing.selectedId = selectedKind === "drawing" ? selectedId : null;
      state.alerts.selectedId = selectedKind === "alert" ? selectedId : null;
      state.optionTargets.selectedId = selectedKind === "option-target" ? selectedId : null;
    }

    const view = {
      barsVisible: storedBarsVisibleForInstrument(initialInstrumentId, initialTimeframe),
      offset: 0,
      priceZoom: 1,
      rightGapManual: false,
      rightGapBars: DEFAULT_RIGHT_GAP_BARS,
      followLatest: true,
      historyPullOffsetBars: 0,
      dragActive: false,
      dragCurrentDeltaBars: 0,
      dragStartHistoryPullOffsetBars: 0,
      dragStartX: 0,
      dragStartY: 0,
      dragStartOffset: 0,
      dragStartPriceZoom: 1,
      dragStartPriceShift: 0,
      dragStartRightGapBars: DEFAULT_RIGHT_GAP_BARS,
      dragStep: 8,
      smoothOffsetBars: 0,
      priceShift: 0,
    };
    const layout = {
      workspaceDockWidth: 300,
      workspaceDockSide: "right",
      gexSidebarPlacement: "chart",
      gexSidebarWidth: 248,
      sideWidth: 360,
      volumeHeight: 160,
    };
