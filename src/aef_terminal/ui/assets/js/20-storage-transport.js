    const SERVER_STORAGE_MEMORY_TTL_MS = 5000;
    const SERVER_STORAGE_MEMORY_MAX_ENTRIES = 8;
    const serverStorageMemory = new Map();
    let serverStorageLoadController = null;

    function pruneServerStorageMemory() {
      if (serverStorageMemory.size <= SERVER_STORAGE_MEMORY_MAX_ENTRIES) return;
      const removable = Array.from(serverStorageMemory.entries())
        .filter(([, entry]) => !entry?.promise)
        .sort((left, right) => Number(left[1]?.at || 0) - Number(right[1]?.at || 0));
      const overflow = serverStorageMemory.size - SERVER_STORAGE_MEMORY_MAX_ENTRIES;
      removable.slice(0, overflow).forEach(([key]) => serverStorageMemory.delete(key));
    }

    function validateExactServerStorageResponse(payload, expectedScope, options = {}) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new TypeError("SERVER_STORAGE_RESPONSE_INVALID: response must be an object");
      }
      if (payload.ok !== true) {
        throw new TypeError("SERVER_STORAGE_RESPONSE_INVALID: ok must be true");
      }
      const expected = {
        instrument_id: exactIdentityText(expectedScope?.instrument_id),
        route_fingerprint: exactIdentityText(expectedScope?.route_fingerprint),
        interval: exactIdentityText(expectedScope?.interval),
        provider: exactIdentityText(expectedScope?.provider),
        provider_contract_id: exactIdentityText(expectedScope?.provider_contract_id),
      };
      if (!expected.interval) {
        throw new TypeError("SERVER_STORAGE_SCOPE_INVALID: interval is required");
      }
      const hasQualifiedScope = Boolean(expected.instrument_id);
      if (
        hasQualifiedScope !== Boolean(expected.route_fingerprint)
        || hasQualifiedScope !== Boolean(expected.provider)
        || hasQualifiedScope !== Boolean(expected.provider_contract_id)
      ) {
        throw new TypeError("SERVER_STORAGE_SCOPE_INVALID: qualified scope is incomplete");
      }
      for (const field of [
        "instrument_id",
        "route_fingerprint",
        "provider",
        "provider_contract_id",
      ]) {
        if (exactIdentityText(payload[field]) !== expected[field]) {
          throw new TypeError(`SERVER_STORAGE_SCOPE_MISMATCH: ${field}`);
        }
      }
      if (exactIdentityText(payload.interval) !== expected.interval) {
        throw new TypeError("SERVER_STORAGE_SCOPE_MISMATCH: interval");
      }

      const responseKind = options.kind === "alerts" ? "alerts" : "storage";
      let drawings = null;
      let alerts = null;
      if (responseKind === "storage") {
        if (!Array.isArray(payload.drawings)) {
          throw new TypeError("DRAWING_ROWS_INVALID: drawings must be an array");
        }
        const drawingCopies = payload.drawings.map(item => {
          if (!item || typeof item !== "object" || Array.isArray(item)) return item;
          const copy = { ...item };
          if (Array.isArray(item.points)) {
            copy.points = item.points.map(point => (
              point && typeof point === "object" && !Array.isArray(point) ? { ...point } : point
            ));
          }
          if (item.offsetPoint && typeof item.offsetPoint === "object" && !Array.isArray(item.offsetPoint)) {
            copy.offsetPoint = { ...item.offsetPoint };
          }
          return copy;
        });
        drawings = normalizeDrawingObjects(drawingCopies);
        if (options.drawings === false && drawings.length) {
          throw new TypeError("SERVER_STORAGE_RESPONSE_INVALID: unrequested drawings returned");
        }
        for (const drawing of drawings) {
          for (const field of [
            "instrument_id",
            "route_fingerprint",
            "provider",
            "provider_contract_id",
          ]) {
            if (exactIdentityText(drawing[field]) !== expected[field]) {
              throw new TypeError(`DRAWING_SCOPE_MISMATCH: ${drawing.id} ${field}`);
            }
          }
        }
      }

      if (responseKind === "alerts" || responseKind === "storage") {
        if (!Array.isArray(payload.alerts)) {
          throw new TypeError("PRICE_ALERT_ROWS_INVALID: alerts must be an array");
        }
        alerts = payload.alerts.map(alert => normalizePriceAlert(alert));
        if (responseKind === "storage" && options.alerts === false && alerts.length) {
          throw new TypeError("SERVER_STORAGE_RESPONSE_INVALID: unrequested alerts returned");
        }
        for (const alert of alerts) {
          for (const [field, expectedValue] of Object.entries({
            instrument_id: expected.instrument_id,
            route_fingerprint: expected.route_fingerprint,
            timeframe: expected.interval,
            provider: expected.provider,
            provider_contract_id: expected.provider_contract_id,
          })) {
            if (exactIdentityText(alert[field]) !== expectedValue) {
              throw new TypeError(`PRICE_ALERT_SCOPE_MISMATCH: ${alert.id} ${field}`);
            }
          }
        }
      }
      if (responseKind === "alerts") {
        if (!Number.isInteger(payload.count) || payload.count !== alerts.length) {
          throw new TypeError("PRICE_ALERT_COUNT_MISMATCH");
        }
        if (
          Number.isInteger(options.deletedCount)
          && (!Number.isInteger(payload.deleted) || payload.deleted !== options.deletedCount)
        ) {
          throw new TypeError("PRICE_ALERT_DELETED_COUNT_MISMATCH");
        }
      }
      return {
        ...payload,
        ...(drawings === null ? {} : { drawings }),
        ...(alerts === null ? {} : { alerts }),
      };
    }

    async function fetchServerStorage(instrumentId = state.instrumentId, interval = state.timeframe, options = {}) {
      const identity = exactIdentityText(instrumentId);
      const isolated = options.isolated === true;
      const expectedRouteFingerprint = exactIdentityText(
        Object.prototype.hasOwnProperty.call(options, "expectedRouteFingerprint")
          ? options.expectedRouteFingerprint
          : instrumentRouteFingerprint(),
      );
      const params = new URLSearchParams({
        instrument_id: identity,
        route_fingerprint: expectedRouteFingerprint,
        interval,
      });
      if (options.settings === false) params.set("settings", "false");
      if (options.drawings === false) params.set("drawings", "false");
      if (options.alerts === false) params.set("alerts", "false");
      const key = JSON.stringify([
        identity,
        expectedRouteFingerprint,
        String(interval || ""),
        options.settings === false ? "no-settings" : "settings",
        options.drawings === false ? "no-drawings" : "drawings",
        options.alerts === false ? "no-alerts" : "alerts",
        isolated ? "isolated" : "shared-status",
      ]);
      const now = Date.now();
      const cached = serverStorageMemory.get(key);
      if (cached?.promise) return cached.promise;
      if (
        !options.force
        && cached?.payload
        && now - Number(cached.at || 0) <= SERVER_STORAGE_MEMORY_TTL_MS
      ) {
        return cached.payload;
      }
      const scopeInstrument = identity ? instrumentForId(identity) : null;
      if (identity && !scopeInstrument) {
        throw new TypeError("SERVER_STORAGE_SCOPE_INVALID: qualified instrument is not loaded");
      }
      const expectedScope = {
        instrument_id: identity,
        route_fingerprint: expectedRouteFingerprint,
        interval: String(interval || ""),
        provider: identity ? exactIdentityText(scopeInstrument?.provider) : "",
        provider_contract_id: identity ? exactIdentityText(scopeInstrument?.session_contract_id) : "",
      };
      const request = fetchJson(`/api/storage?${params.toString()}`, {
        cache: "no-store",
        signal: options.signal,
        telemetryReason: String(
          options.reason
          || (identity ? "route_snapshot" : "settings_snapshot"),
        ),
      })
        .then(payload => {
          if (payload?.ok === false) {
            const message = apiErrorMessage(payload, "server storage load failed");
            if (!isolated) {
              state.storageStatus = { ok: false, message, payload };
              setUiNotice("storage", "STORAGE_LOAD_FAILED", `Storage error: ${message}`, {
                state: "error",
                routeScoped: false,
              });
              setScreenerStateLabel();
            }
            const error = new Error(message);
            error.payload = normalizeErrorPayload(payload) || payload;
            throw error;
          }
          const admitted = validateExactServerStorageResponse(payload, expectedScope, {
            kind: "storage",
            drawings: options.drawings !== false,
            alerts: options.alerts !== false,
          });
          if (!isolated) {
            state.storageStatus = { ok: true, message: "" };
            clearUiNotice("storage");
          }
          if (serverStorageMemory.get(key)?.promise === request) {
            serverStorageMemory.set(key, {
              at: Date.now(),
              payload: admitted,
              promise: null,
            });
            pruneServerStorageMemory();
          }
          return admitted;
        })
        .catch(error => {
          if (serverStorageMemory.get(key)?.promise === request) {
            serverStorageMemory.delete(key);
          }
          if (error?.name === "AbortError") throw error;
          const message = requestErrorMessage(error, "server storage load failed");
          if (!isolated) {
            state.storageStatus = { ok: false, message };
            setUiNotice("storage", "STORAGE_LOAD_FAILED", `Storage error: ${message}`, {
              state: "error",
              routeScoped: false,
            });
            setScreenerStateLabel();
          }
          throw error;
        });
      serverStorageMemory.set(key, {
        at: now,
        payload: cached?.payload || null,
        promise: request,
      });
      pruneServerStorageMemory();
      return request;
    }
