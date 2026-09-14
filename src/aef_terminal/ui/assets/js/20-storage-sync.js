    let drawingProjectionRefreshTimer = null;
    let drawingProjectionRefreshAt = 0;
    let drawingProjectionRefreshScopeKey = "";
    let drawingProjectionRefreshAttempts = 0;
    const DRAWING_PROJECTION_REFRESH_MAX_ATTEMPTS = 3;

    function sharedObjectSignature(value) {
      try {
        return JSON.stringify(value || []);
      } catch {
        return `${Date.now()}`;
      }
    }

    function scopedObjectSignature(instrumentId, timeframe, signature) {
      return JSON.stringify([
        exactIdentityText(instrumentId || ""),
        String(timeframe || ""),
        String(signature || ""),
      ]);
    }

    function priceAlertCommandKey(instrumentId, routeFingerprint, alertId) {
      return JSON.stringify([
        exactIdentityText(instrumentId || ""),
        exactIdentityText(routeFingerprint || ""),
        exactIdentityText(alertId || ""),
      ]);
    }

    function rememberServerPriceAlertGenerations(alerts) {
      for (const alert of alerts) {
        const alertId = exactIdentityText(alert?.id || "");
        const instrumentId = exactIdentityText(alert?.instrument_id || "");
        const routeFingerprint = exactIdentityText(alert?.route_fingerprint || "");
        const generation = Number(alert?.rearmedAt);
        if (!alertId || !instrumentId || !routeFingerprint || !Number.isInteger(generation) || generation < 0) {
          throw new TypeError("PRICE_ALERT_GENERATION_INVALID: server alert generation is required");
        }
        const key = priceAlertCommandKey(instrumentId, routeFingerprint, alertId);
        const remembered = Number(serverAlertCommandGenerations.get(key));
        if (!Number.isInteger(remembered) || generation >= remembered) {
          serverAlertCommandGenerations.set(key, generation);
          serverAlertCommandTargets.set(key, alertId);
        }
      }
    }

    function priceAlertWritePendingForScope(instrumentId, timeframe, routeFingerprint) {
      const scopeKey = scopedObjectSignature(instrumentId, timeframe, routeFingerprint);
      return Number(serverPendingAlertWrites.get(scopeKey) || 0) > 0;
    }

    function localObjectEditActive() {
      const drawingScopeKey = scopedObjectSignature(
        state.instrumentId,
        state.timeframe,
        instrumentRouteFingerprint(),
      );
      return Boolean(
        state.drawing.drag
        || state.drawing.draft
        || state.alerts.drag
        || state.optionTargets?.draggingId
        || Date.now() < Number(state.drawing.localEditUntil || 0)
        || (typeof optionTargetMutationPending === "function" && optionTargetMutationPending())
        || serverDirtyDrawingScopes.has(drawingScopeKey)
      );
    }

    function sharedObjectsActiveForPolling() {
      if (localObjectEditActive()) return true;
      return sharedObjectsWorkSurfaceActive();
    }

    function sharedObjectsWorkSurfaceActive() {
      if (state.sideTab === "alerts") return true;
      if (state.drawing?.selectedId || state.alerts?.selectedId || state.optionTargets?.selectedId) return true;
      if (state.drawing?.tool && state.drawing.tool !== "cursor") return true;
      return false;
    }

    function storagePollingActive(options = {}) {
      if (
        state.instrumentSelectionStatus?.state !== "resolved"
        || !instrumentForId()
        || !instrumentRouteFingerprint()
      ) return false;
      if (options.force) return true;
      if (state.serverSleeping || document.visibilityState !== "visible") return false;
      if (options.activeOwner !== true && !backgroundPollingActive()) return false;
      return sharedObjectsActiveForPolling();
    }

    function markDrawingDeleted(id) {
      const key = String(id || "");
      if (!key) return;
      state.drawing.deletedIds = state.drawing.deletedIds || {};
      state.drawing.deletedIds[key] = Date.now();
    }

    function drawingDefinitionSignature(drawings = state.drawing.objects) {
      return sharedObjectSignature(durableDrawingObjects(drawings));
    }

    function retainLastValidatedDrawingRuntime(drawings) {
      const currentById = new Map(
        (state.drawing.objects || [])
          .filter(object => object && typeof object === "object" && object.id)
          .map(object => [String(object.id), object]),
      );
      for (const object of drawings) {
        const current = currentById.get(String(object?.id || ""));
        if (!current) continue;
        if (drawingDefinitionSignature([current]) !== drawingDefinitionSignature([object])) continue;
        const resolution = normalizedDrawingAnchorResolution(object.anchorResolution);
        const projection = normalizedDrawingAnchorProjection(object.anchorProjection);
        if (resolution?.status === "resolved" && projection) continue;
        if (current.anchorProjection) object.anchorProjection = current.anchorProjection;
        if (normalizedDrawingAnchorResolution(current.anchorResolution)?.status === "resolved") {
          object.anchorResolution = current.anchorResolution;
        }
        if (current.geometryIntegrity?.status === "invalid") {
          object.geometryIntegrity = current.geometryIntegrity;
        }
      }
      return drawings;
    }

    function applyServerDrawings(drawings, options = {}) {
      if (!Array.isArray(drawings)) {
        throw new TypeError("DRAWING_ROWS_INVALID: drawings must be an array");
      }
      if (options.instrumentId && exactIdentityText(options.instrumentId) !== exactIdentityText(state.instrumentId)) return false;
      if (options.routeFingerprint && exactIdentityText(options.routeFingerprint) !== instrumentRouteFingerprint()) return false;
      if (options.timeframe && String(options.timeframe) !== String(state.timeframe)) return false;
      if (options.reconcileRejectedWrite) {
        const scopeKey = scopedObjectSignature(
          state.instrumentId,
          state.timeframe,
          instrumentRouteFingerprint(),
        );
        if (
          state.drawing.drag
          || state.drawing.draft
          || serverPendingDrawingWrites.has(scopeKey)
          || serverQueuedDrawingWrites.has(scopeKey)
          || serverDirtyDrawingScopes.has(scopeKey)
        ) return false;
      } else if (localObjectEditActive()) return false;
      if (
        !options.reconcileRejectedWrite
        && options.expectedDefinitionSignature
        && drawingDefinitionSignature() !== options.expectedDefinitionSignature
      ) return false;
      const normalized = retainLastValidatedDrawingRuntime(normalizeDrawingObjects(drawings));
      resolveDrawingAnchorsAgainstSnapshot(normalized, state.snapshot);
      const signature = sharedObjectSignature(durableDrawingObjects(normalized));
      const runtimeSignature = sharedObjectSignature(normalized);
      if (
        !options.force
        && signature === serverDrawingSignature
        && runtimeSignature === serverDrawingRuntimeSignature
      ) return false;
      const previousHistorySignature = typeof drawingHistoryDefinitionSignature === "function"
        ? drawingHistoryDefinitionSignature(state.drawing.objects)
        : sharedObjectSignature(state.drawing.objects);
      const nextHistorySignature = typeof drawingHistoryDefinitionSignature === "function"
        ? drawingHistoryDefinitionSignature(normalized)
        : signature;
      const selectedId = state.drawing.selectedId;
      state.drawing.objects = normalized;
      state.drawing.draft = null;
      state.drawing.hoverPoint = null;
      state.drawing.selectedId = state.drawing.objects.some(item => item.id === selectedId) ? selectedId : null;
      state.drawing.persistedCount = state.drawing.objects.length;
      state.drawing.deletedIds = {};
      if (
        drawingProjectionSnapshotReadiness(state.snapshot).ready
        && !state.drawing.objects.some(object => drawingAnchorValidationPending(object, state.snapshot))
      ) {
        completeDrawingProjectionRefresh();
      }
      serverDrawingSignature = signature;
      serverDrawingRuntimeSignature = runtimeSignature;
      const scopeKey = scopedObjectSignature(state.instrumentId, state.timeframe, instrumentRouteFingerprint());
      serverPersistedDrawingSignatures.set(scopeKey, signature);
      serverDirtyDrawingScopes.delete(scopeKey);
      if (previousHistorySignature !== nextHistorySignature && typeof resetDrawingHistory === "function") {
        resetDrawingHistory();
      }
      applyDrawingUi();
      if (typeof touchChartUserObjectsVersion === "function") touchChartUserObjectsVersion();
      return true;
    }

    function applyServerPriceAlerts(alerts, options = {}) {
      if (!Array.isArray(alerts)) {
        throw new TypeError("PRICE_ALERT_ROWS_INVALID: alerts must be an array");
      }
      if (Number.isFinite(Number(options.expectedSyncEpoch))
        && Number(options.expectedSyncEpoch) !== Number(state.alerts.syncEpoch || 0)) return false;
      if (!options.force && localObjectEditActive()) return false;
      const scopeInstrumentId = exactIdentityText(options.instrumentId || state.instrumentId || "");
      const scopeTimeframe = String(options.scopeTimeframe || state.timeframe || "");
      const scopeInstrument = instrumentForId(scopeInstrumentId);
      const scopeRouteFingerprint = scopeInstrument ? instrumentRouteFingerprint(scopeInstrument) : "";
      if (
        options.routeFingerprint
        && exactIdentityText(options.routeFingerprint) !== scopeRouteFingerprint
      ) return false;
      const admittedAlerts = options.runtimeOnly
        ? alerts.map(alert => {
          if (!alert || typeof alert !== "object" || Array.isArray(alert)) {
            throw new TypeError("PRICE_ALERT_RUNTIME_ROW_INVALID: runtime row must be an object");
          }
          return alert;
        })
        : alerts.map(alert => normalizePriceAlert(alert));
      const admittedAlertIds = new Set();
      for (const alert of admittedAlerts) {
        if (
          !exactIdentityText(alert.id)
          || exactIdentityText(alert.instrument_id) !== scopeInstrumentId
          || exactIdentityText(alert.route_fingerprint) !== scopeRouteFingerprint
          || exactIdentityText(alert.timeframe) !== scopeTimeframe
        ) {
          throw new TypeError("PRICE_ALERT_SCOPE_MISMATCH: server alert row is outside the exact requested route");
        }
        if (admittedAlertIds.has(alert.id)) {
          throw new TypeError(`PRICE_ALERT_ID_DUPLICATE: ${alert.id}`);
        }
        admittedAlertIds.add(alert.id);
      }
      const liveAlerts = admittedAlerts.filter(alert => alert.deleted !== true);
      rememberServerPriceAlertGenerations(liveAlerts);
      const scopeKey = scopedObjectSignature(scopeInstrumentId, scopeTimeframe, scopeRouteFingerprint);
      const runtimeScopeKey = scopedObjectSignature(
        scopeInstrumentId,
        scopeTimeframe,
        scopeRouteFingerprint,
      );
      if (options.runtimeOnly) {
        const feedbackReady = serverAlertRuntimeFeedbackReadyScopes.has(runtimeScopeKey);
        const previousById = new Map(state.alerts.rules.map(alert => [String(alert?.id || ""), alert]));
        const runtimeRows = liveAlerts.filter(alert => (
          String(alert?.id || "")
          && exactIdentityText(alert?.instrument_id || "") === scopeInstrumentId
          && exactIdentityText(alert?.route_fingerprint || "") === scopeRouteFingerprint
          && String(alert?.timeframe || "") === scopeTimeframe
        ));
        state.alerts.runtimeByScope.set(runtimeScopeKey, runtimeRows.map(row => ({ ...row })));
        const runtimeById = new Map(runtimeRows.map(row => [String(row.id), row]));
        let changed = false;
        state.alerts.rules = state.alerts.rules.map(alert => {
          if (!priceAlertMatchesScope(alert, scopeInstrumentId, scopeTimeframe)) return alert;
          const runtime = runtimeById.get(String(alert.id || ""));
          if (!runtime) return alert;
          const localRearmedAt = Number(alert.rearmedAt || 0);
          const runtimeRearmedAt = Number(runtime.rearmedAt || 0);
          if (localRearmedAt > runtimeRearmedAt) return alert;
          const next = { ...alert };
          for (const field of PRICE_ALERT_RUNTIME_FIELDS) {
            if (Object.prototype.hasOwnProperty.call(runtime, field)) next[field] = runtime[field];
          }
          if (sharedObjectSignature(next) === sharedObjectSignature(alert)) return alert;
          changed = true;
          return next;
        });
        if (changed) {
          renderAlertManager({ force: Boolean(options.force) });
          if (typeof touchChartUserObjectsVersion === "function") touchChartUserObjectsVersion();
        }
        serverAlertRuntimeFeedbackReadyScopes.add(runtimeScopeKey);
        if (feedbackReady) {
          for (const alert of state.alerts.rules) {
            if (!priceAlertMatchesScope(alert, scopeInstrumentId, scopeTimeframe)) continue;
            const previous = previousById.get(String(alert?.id || ""));
            if (!previous) continue;
            const firedAt = Number(alert?.lastFiredAt || 0);
            const previousFiredAt = Number(previous?.lastFiredAt || 0);
            const newlyFired = firedAt > previousFiredAt
              || (alert?.fired === true && previous?.fired !== true);
            if (!newlyFired) continue;
            const level = Number(alert?.lastFiredLevel ?? alert?.price);
            publishUiFeedback({
              kind: "price_alert",
              category: "alert",
              source: String(alert?.kind || "price"),
              code: "price_alert_triggered",
              entity_id: String(alert?.id || ""),
              instrument_id: scopeInstrumentId,
              route_fingerprint: scopeRouteFingerprint,
              timeframe: scopeTimeframe,
              status: "triggered",
              title: "Price alert triggered",
              detail: [
                String(alert?.label || alert?.kind || "Price alert"),
                Number.isFinite(level) ? `@ ${fmt(level)}` : "",
                String(alert?.direction || "").toUpperCase(),
              ].filter(Boolean).join(" · "),
              ts: alert?.lastFiredEventTs || (firedAt > 0 ? new Date(firedAt).toISOString() : new Date().toISOString()),
              severity: "high",
              tone: "warning",
              target_id: "price-alert-list",
              target_tab: "alerts",
              toast: true,
              attention: true,
            });
          }
        }
        return changed;
      }
      const draggingId = state.alerts.drag;
      const selectedId = state.alerts.selectedId;
      const localDragging = draggingId ? state.alerts.rules.find(alert => alert.id === draggingId) : null;
      const scopedDragging = localDragging && priceAlertMatchesScope(localDragging, scopeInstrumentId, scopeTimeframe);
      let serverRules = dedupePriceAlerts(liveAlerts);
      const signature = sharedObjectSignature(serverRules);
      if (!options.force) {
        const appliedSignature = serverAppliedAlertSignatures.get(scopeKey);
        if (signature === appliedSignature) return false;
      }
      if (scopedDragging) {
        const index = serverRules.findIndex(alert => alert.id === draggingId);
        const preserved = { ...localDragging, dragging: true };
        if (index >= 0) serverRules[index] = preserved;
        else serverRules.push(preserved);
      }
      state.alerts.rules = replacePriceAlertsForScope(state.alerts.rules, serverRules, scopeInstrumentId, scopeTimeframe);
      state.alerts.drag = draggingId || null;
      state.alerts.selectedId = scopedDragging
        ? draggingId
        : state.alerts.rules.some(alert => alert.id === selectedId) ? selectedId : null;
      serverAppliedAlertSignatures.set(scopeKey, signature);
      renderAlertManager({ force: Boolean(options.force) });
      if (typeof touchChartUserObjectsVersion === "function") touchChartUserObjectsVersion();
      const cachedRuntime = state.alerts.runtimeByScope.get(runtimeScopeKey);
      if (
        Array.isArray(cachedRuntime)
        && state.transport.quote.alertRuntimeReady
        && !state.transport.quote.alertRuntimeWarning
        && quoteStreamOpen()
        && !quoteStreamStale()
      ) {
        applyServerPriceAlerts(cachedRuntime, {
          force: true,
          instrumentId: scopeInstrumentId,
          routeFingerprint: scopeRouteFingerprint,
          scopeTimeframe,
          runtimeOnly: true,
        });
      }
      return true;
    }

    async function loadServerStorage() {
      const requestedInstrumentId = state.instrumentId;
      const requestedRouteFingerprint = instrumentRouteFingerprint();
      const requestedTimeframe = state.timeframe;
      if (!requestedInstrumentId || !requestedRouteFingerprint) {
        applyServerSettings(serverSettings, { preserveNavigation: true });
        if (["empty", "unresolved"].includes(state.instrumentSelectionStatus?.state)) return null;
        throw new Error("resolved instrument selection is missing its exact route fingerprint");
      }
      const loadKey = JSON.stringify([
        requestedInstrumentId,
        requestedRouteFingerprint,
        requestedTimeframe,
      ]);
      if (serverStorageLoadController?.key === loadKey) return null;
      if (serverStorageLoadController) {
        serverStorageLoadController.controller.abort("storage scope superseded");
      }
      const controller = new AbortController();
      const controllerEntry = { key: loadKey, controller };
      serverStorageLoadController = controllerEntry;
      let storage = null;
      try {
        storage = await fetchServerStorage(requestedInstrumentId, requestedTimeframe, {
          settings: false,
          expectedRouteFingerprint: requestedRouteFingerprint,
          signal: controller.signal,
          reason: "route_boot",
        });
        applyServerSettings(serverSettings, { preserveNavigation: true });
        if (state.instrumentId !== requestedInstrumentId || instrumentRouteFingerprint() !== requestedRouteFingerprint || state.timeframe !== requestedTimeframe) {
          storage = await fetchServerStorage(state.instrumentId, state.timeframe, {
            settings: false,
            expectedRouteFingerprint: instrumentRouteFingerprint(),
            signal: controller.signal,
            reason: "route_superseded",
          });
        }
        applyServerDrawings(storage.drawings, {
          force: true,
          instrumentId: storage.instrument_id,
          routeFingerprint: storage.route_fingerprint,
          timeframe: storage.interval,
        });
        await fetchOptionTargets({ force: true });
        ensureOptionTargetPulseRender();
        applyServerPriceAlerts(storage.alerts, {
          force: true,
          instrumentId: storage.instrument_id,
          routeFingerprint: storage.route_fingerprint,
          scopeTimeframe: storage.interval,
        });
      } catch (error) {
        if (error?.name === "AbortError") return null;
        const message = requestErrorMessage(error, "server storage load failed");
        state.storageStatus = { ok: false, message };
        setUiNotice("storage", "STORAGE_SYNC_FAILED", `Storage error: ${message}`, {
          state: "error",
          routeScoped: false,
        });
        setScreenerStateLabel();
        console.warn("server storage load failed", message);
        throw error;
      } finally {
        if (serverStorageLoadController === controllerEntry) {
          serverStorageLoadController = null;
        }
        flushServerSettings();
      }
    }

    function commitDrawingPersistRequest(request) {
      let writeFailed = false;
      const promise = fetchJson("/api/drawings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instrument_id: request.instrumentId,
          route_fingerprint: request.routeFingerprint,
          interval: request.timeframe,
          drawings: request.drawings,
          allow_mass_delete: request.allowMassDelete,
          deleted_ids: request.deletedIds,
        }),
      }).then(payload => {
        if (payload?.ok === false) throw new Error(apiErrorMessage(payload, "server rejected drawings save"));
        if (exactIdentityText(payload?.instrument_id || "") !== request.instrumentId) throw new Error("drawings instrument_id mismatch");
        if (exactIdentityText(payload?.route_fingerprint || "") !== request.routeFingerprint) throw new Error("drawings route_fingerprint mismatch");
        if (String(payload?.interval || "") !== request.timeframe) throw new Error("drawings interval mismatch");
        if (Number(payload?.count) !== request.drawings.length) throw new Error("drawings committed count mismatch");
        serverPersistedDrawingSignatures.set(request.scopeKey, request.drawingSignature);
        if (!serverQueuedDrawingWrites.has(request.scopeKey)) serverDirtyDrawingScopes.delete(request.scopeKey);
        if (
          state.instrumentId === request.instrumentId
          && instrumentRouteFingerprint() === request.routeFingerprint
          && state.timeframe === request.timeframe
          && sharedObjectSignature(durableDrawingObjects(state.drawing.objects)) === request.drawingSignature
        ) {
          state.drawing.persistedCount = request.drawings.length;
          serverDrawingSignature = request.drawingSignature;
          if (request.allowMassDelete) state.drawing.deletedIds = {};
          else for (const id of request.deletedIds) delete state.drawing.deletedIds[id];
          if (!serverQueuedDrawingWrites.has(request.scopeKey)) {
            notifyIndicatorDrawingCommit({
              scopeKey: request.scopeKey,
              instrumentId: request.instrumentId,
              routeFingerprint: request.routeFingerprint,
              timeframe: request.timeframe,
              drawings: request.drawings,
              drawingSignature: request.drawingSignature,
            });
          }
        }
        broadcastSharedObjectsChanged(request.instrumentId, request.timeframe, "drawings", request.routeFingerprint);
        return payload;
      }).catch(error => {
        writeFailed = true;
        const message = requestErrorMessage(error, "server drawings save failed");
        console.warn("server drawings save failed", message);
        if (
          state.instrumentId === request.instrumentId
          && instrumentRouteFingerprint() === request.routeFingerprint
          && state.timeframe === request.timeframe
        ) showBrowserToast(`Drawing save failed: ${message}`);
        return null;
      }).finally(() => {
        const active = serverPendingDrawingWrites.get(request.scopeKey);
        if (active?.promise === promise) serverPendingDrawingWrites.delete(request.scopeKey);
        const queued = serverQueuedDrawingWrites.get(request.scopeKey);
        if (queued) {
          serverQueuedDrawingWrites.delete(request.scopeKey);
          return commitDrawingPersistRequest({
            ...queued,
            reconcileAfterSettle: Boolean(
              request.reconcileAfterSettle
              || writeFailed
              || queued.reconcileAfterSettle
            ),
          });
          return;
        }
        serverDirtyDrawingScopes.delete(request.scopeKey);
        if (
          (request.reconcileAfterSettle || writeFailed)
          && state.instrumentId === request.instrumentId
          && instrumentRouteFingerprint() === request.routeFingerprint
          && state.timeframe === request.timeframe
        ) {
          return loadDrawingsFromServerForCurrent({
            force: true,
            reason: "drawing_write_reconcile",
            reconcileRejectedWrite: true,
          });
        }
      });
      serverPendingDrawingWrites.set(request.scopeKey, { ...request, promise });
      return promise;
    }

    function persistDrawingsToServer(options = {}) {
      if (!serverStorageReady) return null;
      const requestInstrumentId = state.instrumentId;
      const requestTimeframe = state.timeframe;
      const requestRouteFingerprint = instrumentRouteFingerprint();
      if (!requestInstrumentId || !requestRouteFingerprint) return null;
      const drawings = JSON.parse(JSON.stringify(durableDrawingObjects(state.drawing.objects)));
      const drawingSignature = sharedObjectSignature(drawings);
      const scopeKey = scopedObjectSignature(requestInstrumentId, requestTimeframe, requestRouteFingerprint);
      const active = serverPendingDrawingWrites.get(scopeKey);
      const queued = serverQueuedDrawingWrites.get(scopeKey);
      if (!active && serverPersistedDrawingSignatures.get(scopeKey) === drawingSignature) {
        serverDirtyDrawingScopes.delete(scopeKey);
        return null;
      }
      serverDirtyDrawingScopes.add(scopeKey);
      const request = {
        scopeKey,
        instrumentId: requestInstrumentId,
        routeFingerprint: requestRouteFingerprint,
        timeframe: requestTimeframe,
        drawings,
        drawingSignature,
        allowMassDelete: Boolean(options.allowMassDelete || active?.allowMassDelete || queued?.allowMassDelete),
        deletedIds: [...new Set([
          ...(Array.isArray(active?.deletedIds) ? active.deletedIds : []),
          ...(Array.isArray(queued?.deletedIds) ? queued.deletedIds : []),
          ...(Array.isArray(options.deletedIds) ? options.deletedIds : []),
        ].filter(Boolean))],
      };
      if (active) {
        if (active.drawingSignature === drawingSignature) {
          serverQueuedDrawingWrites.delete(scopeKey);
          return active.promise;
        }
        serverQueuedDrawingWrites.set(scopeKey, request);
        return active.promise;
      }
      return commitDrawingPersistRequest(request);
    }

    function priceAlertCommandPayload(action, alert, scope = {}) {
      const source = alert && typeof alert === "object" ? priceAlertClientPayload(alert) : null;
      const instrumentId = exactIdentityText(source?.instrument_id || scope.instrument_id || "");
      const routeFingerprint = exactIdentityText(source?.route_fingerprint || scope.route_fingerprint || "");
      const timeframe = String(source?.timeframe || scope.timeframe || "");
      if (!instrumentId || !routeFingerprint || !timeframe) {
        throw new TypeError("PRICE_ALERT_COMMAND_SCOPE_INVALID");
      }
      if (action === "delete_scope") {
        return { instrument_id: instrumentId, route_fingerprint: routeFingerprint, timeframe };
      }
      const alertId = exactIdentityText(source?.id || "");
      if (!alertId) throw new TypeError("PRICE_ALERT_COMMAND_ID_INVALID");
      const identity = {
        alert_type: "price",
        alert_id: alertId,
        instrument_id: instrumentId,
        route_fingerprint: routeFingerprint,
        expected_rearmed_at: source.rearmedAt,
      };
      if (action === "create") return { ...source, ...identity };
      if (action === "update") {
        return {
          ...identity,
          price: source.price,
          direction: source.direction,
          label: source.label,
          toleranceAtr: source.toleranceAtr,
          tolerancePoints: source.tolerancePoints,
          rearmMinutes: source.rearmMinutes,
        };
      }
      return identity;
    }

    function reconcileCommittedPriceAlert(action, requestAlert, response) {
      if (action === "delete_scope") {
        const instrumentId = exactIdentityText(response?.instrument_id || "");
        const timeframe = String(response?.timeframe || "");
        state.alerts.rules = state.alerts.rules.filter(alert => (
          !priceAlertMatchesScope(alert, instrumentId, timeframe)
        ));
        state.alerts.selectedId = null;
        return;
      }
      if (["delete", "disable"].includes(action)) {
        const requestedId = exactIdentityText(requestAlert?.id || "");
        state.alerts.rules = state.alerts.rules.filter(alert => alert.id !== requestedId);
        if (state.alerts.selectedId === requestedId) state.alerts.selectedId = null;
        return;
      }
      const saved = normalizePriceAlert(response?.payload);
      const requestedId = exactIdentityText(requestAlert?.id || "");
      const semanticKey = alertSemanticKey(saved);
      if (!semanticKey) throw new TypeError("PRICE_ALERT_DEFINITION_IDENTITY_INVALID");
      state.alerts.rules = state.alerts.rules.filter(alert => (
        exactIdentityText(alert?.id || "") !== requestedId
        && exactIdentityText(alert?.id || "") !== saved.id
        && alertSemanticKey(alert) !== semanticKey
      ));
      state.alerts.rules.push(saved);
      if (state.alerts.selectedId === requestedId) state.alerts.selectedId = saved.id;
    }

    function clearCommittedPriceAlertScope(instrumentId, routeFingerprint) {
      for (const key of serverAlertCommandGenerations.keys()) {
        const identity = JSON.parse(key);
        if (identity[0] !== instrumentId || identity[1] !== routeFingerprint) continue;
        serverAlertCommandGenerations.delete(key);
        serverAlertCommandTargets.delete(key);
      }
    }

    function commitPriceAlertCommand(action, alert = null, scope = {}) {
      if (!serverStorageReady) return Promise.resolve(null);
      let commandPayload;
      try {
        commandPayload = priceAlertCommandPayload(action, alert, scope);
      } catch (error) {
        console.warn("price alert command rejected", error);
        return Promise.resolve(null);
      }
      const instrumentId = exactIdentityText(commandPayload.instrument_id || "");
      const routeFingerprint = exactIdentityText(commandPayload.route_fingerprint || "");
      const timeframe = String(commandPayload.timeframe || alert?.timeframe || "");
      const scopeKey = scopedObjectSignature(instrumentId, timeframe, routeFingerprint);
      const alertKey = action === "delete_scope"
        ? ""
        : priceAlertCommandKey(instrumentId, routeFingerprint, commandPayload.alert_id);
      serverPendingAlertWrites.set(
        scopeKey,
        Number(serverPendingAlertWrites.get(scopeKey) || 0) + 1,
      );
      const previous = serverAlertCommandChains.get(scopeKey) || Promise.resolve();
      const operation = previous.catch(() => null).then(() => {
        const committedGeneration = serverAlertCommandGenerations.get(alertKey);
        const committedAlertId = exactIdentityText(serverAlertCommandTargets.get(alertKey) || "");
        const requestPayload = {
          ...commandPayload,
          ...(committedAlertId ? { alert_id: committedAlertId } : {}),
          ...(Number.isInteger(committedGeneration)
            ? { expected_rearmed_at: committedGeneration }
            : {}),
        };
        return fetchJson("/api/alerts/command", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, payload: requestPayload }),
        });
      }).then(response => {
        if (response?.ok !== true) {
          throw new Error(apiErrorMessage(response, "server rejected price alert command"));
        }
        const committedGeneration = Number(response?.payload?.rearmedAt);
        const committedAlertId = exactIdentityText(response?.payload?.id || "");
        if (alertKey && Number.isInteger(committedGeneration) && committedAlertId) {
          serverAlertCommandGenerations.set(alertKey, committedGeneration);
          serverAlertCommandTargets.set(alertKey, committedAlertId);
        } else if (["delete", "disable"].includes(action)) {
          serverAlertCommandGenerations.delete(alertKey);
          serverAlertCommandTargets.delete(alertKey);
        } else if (action === "delete_scope") {
          clearCommittedPriceAlertScope(instrumentId, routeFingerprint);
        }
        reconcileCommittedPriceAlert(action, alert, response);
        state.alerts.syncEpoch = Number(state.alerts.syncEpoch || 0) + 1;
        broadcastSharedObjectsChanged(instrumentId, timeframe, "alerts", routeFingerprint);
        renderAlertManager({ force: true });
        refreshPriceAlertLayers();
        return response;
      }).catch(error => {
        const message = requestErrorMessage(error, "price alert command failed");
        console.warn("price alert command failed", message);
        showBrowserToast(`Alert save failed: ${message}`);
        return null;
      }).finally(() => {
        const pending = Number(serverPendingAlertWrites.get(scopeKey) || 0) - 1;
        if (pending > 0) serverPendingAlertWrites.set(scopeKey, pending);
        else serverPendingAlertWrites.delete(scopeKey);
        if (serverAlertCommandChains.get(scopeKey) === operation) {
          serverAlertCommandChains.delete(scopeKey);
        }
        if (!serverPendingAlertWrites.has(scopeKey)) {
          void loadDrawingsFromServerForCurrent({ force: true, reason: "alert_command_reconcile" });
        }
      });
      serverAlertCommandChains.set(scopeKey, operation);
      return operation;
    }

    async function loadDrawingsFromServerForCurrent(options = {}) {
      if (!serverStorageReady) return;
      const instrumentId = state.instrumentId;
      const routeFingerprint = instrumentRouteFingerprint();
      const interval = state.timeframe;
      const reconcileRejectedWrite = options.reconcileRejectedWrite === true;
      const expectedDefinitionSignature = reconcileRejectedWrite
        ? ""
        : drawingDefinitionSignature();
      const alertSyncEpoch = Number(state.alerts.syncEpoch || 0);
      const alertWritePending = priceAlertWritePendingForScope(instrumentId, interval, routeFingerprint);
      try {
        const storage = await fetchServerStorage(instrumentId, interval, {
          settings: false,
          alerts: !alertWritePending,
          expectedRouteFingerprint: routeFingerprint,
          force: options.force === true,
          reason: options.reason,
        });
        if (state.instrumentId !== instrumentId || instrumentRouteFingerprint() !== routeFingerprint || state.timeframe !== interval) return;
        const drawingVersionCurrent = reconcileRejectedWrite
          || drawingDefinitionSignature() === expectedDefinitionSignature;
        const drawingsChanged = drawingVersionCurrent
          ? applyServerDrawings(storage.drawings, {
              force: true,
              instrumentId: storage.instrument_id,
              routeFingerprint: storage.route_fingerprint,
              timeframe: storage.interval,
              expectedDefinitionSignature,
              reconcileRejectedWrite,
            })
          : false;
        if (!drawingVersionCurrent && options.reason === "drawing_projection_refresh") {
          scheduleDrawingProjectionRefresh();
        }
        const alertsChanged = !alertWritePending ? applyServerPriceAlerts(storage.alerts, {
          force: true,
          instrumentId: storage.instrument_id,
          routeFingerprint: storage.route_fingerprint,
          scopeTimeframe: storage.interval,
          expectedSyncEpoch: alertSyncEpoch,
        }) : false;
        if (drawingsChanged || alertsChanged) requestObjectLayerRedraw("server objects load", { force: true });
      } catch (error) {
        console.warn("server drawings load failed", requestErrorMessage(error, "server drawings load failed"));
        if (options.reason === "drawing_projection_refresh") {
          scheduleDrawingProjectionRefresh();
        }
      }
    }

    function completeDrawingProjectionRefresh() {
      if (drawingProjectionRefreshTimer) clearTimeout(drawingProjectionRefreshTimer);
      drawingProjectionRefreshTimer = null;
      drawingProjectionRefreshScopeKey = "";
      drawingProjectionRefreshAttempts = 0;
    }

    function scheduleDrawingProjectionRefresh() {
      if (!serverStorageReady) return false;
      const readiness = drawingProjectionSnapshotReadiness(state.snapshot);
      if (!readiness.ready) return false;
      const scopeKey = drawingProjectionSnapshotCacheKey(state.snapshot);
      if (drawingProjectionRefreshTimer && scopeKey !== drawingProjectionRefreshScopeKey) {
        clearTimeout(drawingProjectionRefreshTimer);
        drawingProjectionRefreshTimer = null;
      }
      if (scopeKey !== drawingProjectionRefreshScopeKey) {
        drawingProjectionRefreshScopeKey = scopeKey;
        drawingProjectionRefreshAttempts = 0;
        drawingProjectionRefreshAt = 0;
      }
      if (drawingProjectionRefreshTimer) return false;
      if (drawingProjectionRefreshAttempts >= DRAWING_PROJECTION_REFRESH_MAX_ATTEMPTS) return false;
      const retryDelay = Math.min(2000, 250 * (2 ** drawingProjectionRefreshAttempts));
      const delay = Math.max(80, retryDelay - (Date.now() - drawingProjectionRefreshAt));
      drawingProjectionRefreshTimer = setTimeout(() => {
        drawingProjectionRefreshTimer = null;
        if (
          !drawingProjectionSnapshotReadiness(state.snapshot).ready
          || drawingProjectionSnapshotCacheKey(state.snapshot) !== drawingProjectionRefreshScopeKey
        ) return;
        drawingProjectionRefreshAt = Date.now();
        if (localObjectEditActive()) {
          scheduleDrawingProjectionRefresh();
          return;
        }
        drawingProjectionRefreshAttempts += 1;
        void loadDrawingsFromServerForCurrent({
          force: true,
          reason: "drawing_projection_refresh",
        });
      }, delay);
      return true;
    }

    function refreshSharedObjectsFromServer(options = {}) {
      if (!serverStorageReady || localObjectEditActive()) return Promise.resolve(false);
      if (!storagePollingActive(options)) return Promise.resolve(false);
      const instrumentId = state.instrumentId;
      const routeFingerprint = instrumentRouteFingerprint();
      const interval = state.timeframe;
      const syncKey = JSON.stringify([instrumentId, routeFingerprint, interval]);
      if (sharedObjectSyncInFlight) {
        if (sharedObjectSyncInFlight.key === syncKey) {
          return sharedObjectSyncInFlight.promise;
        }
        sharedObjectSyncInFlight.controller.abort("shared object scope superseded");
      }
      const alertSyncEpoch = Number(state.alerts.syncEpoch || 0);
      const alertWritePending = priceAlertWritePendingForScope(instrumentId, interval, routeFingerprint);
      const controller = new AbortController();
      const request = (async () => {
        try {
          const storage = await fetchServerStorage(instrumentId, interval, {
            settings: options.reconcileSettings === true,
            alerts: !alertWritePending,
            expectedRouteFingerprint: routeFingerprint,
            signal: controller.signal,
            reason: options.reconcileSettings === true
              ? "objects_settings_reconcile"
              : "objects_reconcile",
          });
          if (state.instrumentId !== instrumentId || instrumentRouteFingerprint() !== routeFingerprint || state.timeframe !== interval) return false;
          if (options.reconcileSettings === true) {
            reconcileServerSettingsSnapshot(storage);
          }
          const drawingsChanged = applyServerDrawings(storage.drawings, {
            instrumentId: storage.instrument_id,
            routeFingerprint: storage.route_fingerprint,
            timeframe: storage.interval,
          });
          if (drawingsChanged) {
            const drawingScopeKey = scopedObjectSignature(
              storage.instrument_id,
              storage.interval,
              storage.route_fingerprint,
            );
            notifyIndicatorDrawingCommit({
              scopeKey: drawingScopeKey,
              instrumentId: storage.instrument_id,
              routeFingerprint: storage.route_fingerprint,
              timeframe: storage.interval,
              drawings: state.drawing.objects,
              drawingSignature: serverPersistedDrawingSignatures.get(drawingScopeKey) || "",
            });
          }
          const alertsChanged = !alertWritePending
            ? applyServerPriceAlerts(storage.alerts, {
                instrumentId: storage.instrument_id,
                routeFingerprint: storage.route_fingerprint,
                scopeTimeframe: storage.interval,
                expectedSyncEpoch: alertSyncEpoch,
              })
            : false;
          if (drawingsChanged || alertsChanged) {
            requestObjectLayerRedraw("shared objects sync");
            return true;
          }
        } catch (error) {
          if (error?.name !== "AbortError") {
            console.warn("shared object sync failed", requestErrorMessage(error, "shared object sync failed"));
          }
        }
        return false;
      })();
      const entry = { key: syncKey, controller, promise: request };
      sharedObjectSyncInFlight = entry;
      request.then(
        () => {
          if (sharedObjectSyncInFlight === entry) sharedObjectSyncInFlight = null;
        },
        () => {
          if (sharedObjectSyncInFlight === entry) sharedObjectSyncInFlight = null;
        },
      );
      return request;
    }

    function broadcastSharedObjectsChanged(instrumentId = state.instrumentId, timeframe = state.timeframe, kind = "objects", routeFingerprint = instrumentRouteFingerprint()) {
      try {
        rawLocalStorageSetItem(
          sharedObjectsBroadcastKey(instrumentId, timeframe, routeFingerprint),
          JSON.stringify({ instrument_id: instrumentId, route_fingerprint: routeFingerprint, timeframe, kind, updatedAt: Date.now() }),
        );
      } catch {
        // Broadcast is best-effort; PostgreSQL remains authoritative.
      }
    }

    function applySharedObjectsFromLocalStorageEvent(event) {
      if (!event?.key || event.storageArea !== localStorage) return false;
      const optionTargetDeleteEvent = typeof optionTargetDeleteBroadcastKey === "function" && event.key === optionTargetDeleteBroadcastKey();
      if (!optionTargetDeleteEvent && localObjectEditActive()) return false;
      let changed = false;
      if (event.key === sharedObjectsBroadcastKey()) {
        refreshSharedObjectsFromServer({ force: true });
      } else if (optionTargetDeleteEvent && typeof applyOptionTargetDeleted === "function") {
        try {
          const payload = JSON.parse(event.newValue || "{}");
          changed = applyOptionTargetDeleted(payload);
        } catch {
          changed = false;
        }
      }
      return changed;
    }
