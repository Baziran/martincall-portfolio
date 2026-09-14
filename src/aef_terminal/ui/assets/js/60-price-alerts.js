    function savePriceAlerts() {
      savePriceAlertsForScope(state.instrumentId, state.timeframe);
    }

    function savePriceAlertsForScope(instrumentId = state.instrumentId, timeframe = state.timeframe) {
      const scopeInstrumentId = exactIdentityText(instrumentId ?? state.instrumentId);
      const scopeTimeframe = String(timeframe || state.timeframe || "");
      const scopedRules = dedupePriceAlerts(priceAlertsForScope(state.alerts.rules, scopeInstrumentId, scopeTimeframe));
      state.alerts.rules = replacePriceAlertsForScope(state.alerts.rules, scopedRules, scopeInstrumentId, scopeTimeframe);
      state.alerts.syncEpoch = Number(state.alerts.syncEpoch || 0) + 1;
      touchChartUserObjectsVersion();
      renderAlertManager();
    }

    function refreshPriceAlertLayers(options = {}) {
      if (state.snapshot) {
        requestObjectLayerRedraw("price alerts refreshed", { ...options, force: true });
      } else {
        renderDrawingOverlay();
      }
    }

    function loadPriceAlertsForCurrent() {
      if (!priceAlertWritePendingForScope(state.instrumentId, state.timeframe, instrumentRouteFingerprint())) {
        state.alerts.rules = replacePriceAlertsForScope(
          state.alerts.rules,
          [],
          state.instrumentId,
          state.timeframe,
        );
      }
      state.alerts.drag = null;
      state.alerts.selectedId = null;
      renderAlertManager();
    }

    function findPriceAlert(id) {
      return state.alerts.rules.find(alert => alert.id === id) || null;
    }

    function rearmPriceAlert(alert) {
      if (!alert) return;
      alert.armed = true;
      alert.fired = false;
      alert.cooldownUntil = 0;
      alert.rearmedAt = Date.now();
      const dynamicLevel = alertDynamicLevel(alert, state.snapshot);
      if (Number.isFinite(dynamicLevel)) {
        alert.price = dynamicLevel;
      }
    }

    function emaAlertRearmMinutes(alert = null) {
      const raw = Number(alert?.rearmMinutes ?? state.indicators.ema233.alertRearmMinutes ?? 60);
      return [15, 30, 60].includes(raw) ? raw : 60;
    }

    function updatePriceAlert(id, patch = {}) {
      const alert = findPriceAlert(id);
      if (!alert) return;
      let command = "update";
      if (patch.price !== undefined) {
        const price = Number(patch.price);
        if (!Number.isFinite(price)) return;
        alert.price = price;
        rearmPriceAlert(alert);
      }
      if (patch.direction !== undefined && ["cross", "above", "below"].includes(patch.direction)) {
        alert.direction = patch.direction;
        rearmPriceAlert(alert);
      }
      if (patch.toleranceAtr !== undefined) {
        const value = Number(patch.toleranceAtr);
        if (Number.isFinite(value)) {
          alert.toleranceAtr = clamp(value, 0, 1);
          rearmPriceAlert(alert);
        }
      }
      if (patch.enabled !== undefined) {
        alert.enabled = Boolean(patch.enabled);
        if (alert.enabled) {
          rearmPriceAlert(alert);
        }
        command = alert.enabled ? "enable" : "disable";
      }
      if (patch.rearm) {
        rearmPriceAlert(alert);
        command = "rearm";
      }
      savePriceAlertsForScope(alert.instrument_id, alert.timeframe);
      void commitPriceAlertCommand(command, alert);
      refreshPriceAlertLayers();
    }

    function deletePriceAlert(id) {
      const alert = findPriceAlert(id);
      if (!alert) return;
      const instrumentId = alert.instrument_id;
      const timeframe = alert.timeframe;
      state.alerts.rules = state.alerts.rules.filter(item => item.id !== id);
      if (state.alerts.selectedId === id) state.alerts.selectedId = null;
      savePriceAlertsForScope(instrumentId, timeframe);
      void commitPriceAlertCommand("delete", alert);
      applyDrawingUi();
      refreshPriceAlertLayers();
    }

    function deleteSelectedChartItem() {
      if (selectedOptionTarget()) {
        deleteOptionTarget(state.optionTargets.selectedId);
        return;
      }
      if (selectedPriceAlert()) {
        deletePriceAlert(state.alerts.selectedId);
        return;
      }
      deleteSelectedDrawing();
    }

    function deleteAllPriceAlerts() {
      const count = currentPriceAlerts().length;
      if (!count) return;
      if (!window.confirm(`Delete ${count} price alert(s) for ${state.symbol} ${state.timeframe}?`)) return;
      const routeFingerprint = instrumentRouteFingerprint();
      state.alerts.rules = state.alerts.rules.filter(alert => !priceAlertMatchesScope(alert, state.instrumentId, state.timeframe));
      state.alerts.selectedId = null;
      savePriceAlerts();
      void commitPriceAlertCommand("delete_scope", null, {
        instrument_id: state.instrumentId,
        route_fingerprint: routeFingerprint,
        timeframe: state.timeframe,
      });
      applyDrawingUi();
      refreshPriceAlertLayers();
    }

    function addPriceAlert(price, direction = "cross") {
      const alertPrice = Number(price);
      const canonicalDirection = String(direction || "").toLowerCase();
      if (!Number.isFinite(alertPrice) || !["cross", "above", "below"].includes(canonicalDirection)) return;
      const instrument = instrumentForId(state.instrumentId);
      const createdAt = Date.now();
      const alert = {
        id: `pa-${createBrowserUuidV4()}`,
        instrument_id: state.instrumentId,
        route_fingerprint: instrumentRouteFingerprint(),
        provider: exactIdentityText(instrument?.provider),
        provider_contract_id: exactIdentityText(instrument?.session_contract_id),
        symbol: state.symbol,
        timeframe: state.timeframe,
        price: alertPrice,
        kind: "price",
        label: "",
        direction: canonicalDirection,
        toleranceAtr: 0.08,
        tolerancePoints: 0,
        enabled: true,
        armed: true,
        fired: false,
        cooldownUntil: 0,
        rearmedAt: createdAt,
        rearmMinutes: 60,
        createdAt,
      };
      rearmPriceAlert(alert);
      state.alerts.rules.push(alert);
      selectPriceAlert(alert.id);
      savePriceAlertsForScope(alert.instrument_id, alert.timeframe);
      void commitPriceAlertCommand("create", alert);
      refreshPriceAlertLayers();
    }

    function addEma233TouchAlert() {
      const stateNow = emaTouchState({ kind: "ema233_touch", toleranceAtr: 0.08 }, state.snapshot);
      if (!stateNow || !Number.isFinite(stateNow.level)) {
        showBrowserToast("EMA 233 is not available yet");
        return;
      }
      const instrument = instrumentForId(state.instrumentId);
      const createdAt = Date.now();
      const alert = {
        id: `pa-${createBrowserUuidV4()}`,
        instrument_id: state.instrumentId,
        route_fingerprint: instrumentRouteFingerprint(),
        provider: exactIdentityText(instrument?.provider),
        provider_contract_id: exactIdentityText(instrument?.session_contract_id),
        symbol: state.symbol,
        timeframe: state.timeframe,
        kind: "ema233_touch",
        label: "EMA 233 touch",
        price: stateNow.level,
        direction: "cross",
        toleranceAtr: 0.08,
        tolerancePoints: 0,
        rearmMinutes: emaAlertRearmMinutes(),
        enabled: true,
        armed: true,
        fired: false,
        cooldownUntil: 0,
        rearmedAt: createdAt,
        createdAt,
      };
      state.alerts.rules.push(alert);
      selectPriceAlert(alert.id);
      savePriceAlertsForScope(alert.instrument_id, alert.timeframe);
      void commitPriceAlertCommand("create", alert);
      refreshPriceAlertLayers();
    }

    function addVsaFuelAlert() {
      const price = currentSnapshotPrice(state.snapshot);
      if (!Number.isFinite(price)) {
        showBrowserToast("Price is not available yet");
        return;
      }
      const createdAt = Date.now();
      const instrument = instrumentForId(state.instrumentId);
      const alert = {
        id: `pa-${createBrowserUuidV4()}`,
        instrument_id: state.instrumentId,
        route_fingerprint: instrumentRouteFingerprint(),
        provider: exactIdentityText(instrument?.provider),
        provider_contract_id: exactIdentityText(instrument?.session_contract_id),
        symbol: state.symbol,
        timeframe: state.timeframe,
        kind: "vsa_fuel",
        label: "VSA fuel candle",
        price,
        direction: "cross",
        toleranceAtr: 0,
        tolerancePoints: 0,
        enabled: true,
        armed: true,
        fired: false,
        cooldownUntil: 0,
        rearmedAt: createdAt,
        rearmMinutes: 60,
        createdAt,
      };
      state.alerts.rules.push(alert);
      selectPriceAlert(alert.id);
      savePriceAlertsForScope(alert.instrument_id, alert.timeframe);
      void commitPriceAlertCommand("create", alert);
      refreshPriceAlertLayers();
    }

    function currentSnapshotPrice(snapshot) {
      if (!snapshot?.bars?.length || typeof currentChartDisplayPrice !== "function") return null;
      return currentChartDisplayPrice(snapshot);
    }

    function sendTelegramTestAlert() {
      fetchJson("/api/alerts/telegram/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: `MartinCall Telegram test · ${state.symbol} ${state.timeframe}` }),
        cache: "no-store",
      }).then(data => {
        if (data.ok) {
          showBrowserToast("Telegram test sent");
        } else {
          showBrowserToast(`Telegram test failed: ${data.status || "not configured"}`);
        }
        state.systemHealth = { ...(state.systemHealth || {}), telegram: data };
        renderTelegramAlertStatus();
      }).catch(error => {
        showBrowserToast("Telegram test failed");
        console.warn("telegram test failed", error);
      });
    }

    function hitTestPriceAlert(localX, localY) {
      const geo = priceChartGeometry();
      if (!geo || localX < geo.pad.left || localX > geo.rect.width - geo.pad.right) return null;
      let best = null;
      const canvas = document.getElementById("price-chart");
      const ctx = canvas?.getContext?.("2d");
      if (ctx) ctx.font = "800 10px -apple-system, BlinkMacSystemFont, sans-serif";
      for (const alert of currentPriceAlerts()) {
        if (!alert.enabled) continue;
        const yy = geo.y(Number(alertDynamicLevel(alert, state.snapshot) ?? alert.price));
        if (!Number.isFinite(yy)) continue;
        const selected = alert.id === state.alerts.selectedId;
        const label = priceAlertCanvasLabel(alert, selected);
        const labelW = (ctx ? ctx.measureText(label).width : label.length * 6.2) + 12;
        const layout = priceAlertCanvasLayout(geo.pad, geo.rect.width, yy, labelW);
        const inDelete = localX >= layout.deleteX1 && localX <= layout.deleteX2 && localY >= layout.deleteY1 && localY <= layout.deleteY2;
        const inHandle = localX >= layout.hitX1 && localX <= layout.hitX2 && localY >= layout.hitY1 && localY <= layout.hitY2;
        if (!inDelete && !inHandle) continue;
        const distance = Math.abs(localY - yy);
        const action = inDelete ? "delete" : "drag";
        if (!best || distance < best.distance) best = { alert, action, distance };
      }
      return best || null;
    }

    function startPriceAlertDrag(canvas, event, alert) {
      if (!alert) return;
      if (alert.kind === "ema233_touch") {
        selectPriceAlert(alert.id);
        return;
      }
      const alertId = alert.id;
      const dragAlert = () => {
        let liveAlert = findPriceAlert(alertId);
        if (!liveAlert) {
          liveAlert = alert;
          if (!state.alerts.rules.some(item => item.id === alertId)) state.alerts.rules.push(liveAlert);
        }
        return liveAlert;
      };
      dragAlert();
      selectPriceAlert(alertId);
      state.alerts.drag = alertId;
      dragAlert().dragging = true;
      touchChartUserObjectsVersion();
      if (state.snapshot) renderPriceObjects(state.snapshot, { excludePriceAlertIds: [alertId] });
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
      const rect = canvas.getBoundingClientRect();
      const updateFromEvent = move => {
        const geo = priceChartGeometry();
        if (!geo) return;
        const localY = move.clientY - rect.top;
        const price = geo.maxP - ((localY - geo.pad.top) / geo.priceH) * (geo.maxP - geo.minP);
        if (!Number.isFinite(price)) return;
        const liveAlert = dragAlert();
        liveAlert.price = price;
        rearmPriceAlert(liveAlert);
        liveAlert.dragging = true;
        touchChartUserObjectsVersion();
        if (state.snapshot) renderPriceObjects(state.snapshot, { excludePriceAlertIds: [alertId] });
      };
      const onMove = move => updateFromEvent(move);
      const onUp = move => {
        updateFromEvent(move);
        canvas.classList.remove("dragging");
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        canvas.removeEventListener("pointercancel", onCancel);
        canvas.removeEventListener("lostpointercapture", onCancel);
        state.alerts.drag = null;
        delete dragAlert().dragging;
        savePriceAlertsForScope(dragAlert().instrument_id, dragAlert().timeframe);
        void commitPriceAlertCommand("update", dragAlert());
        refreshPriceAlertLayers();
      };
      const onCancel = () => {
        canvas.classList.remove("dragging");
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        canvas.removeEventListener("pointercancel", onCancel);
        canvas.removeEventListener("lostpointercapture", onCancel);
        state.alerts.drag = null;
        delete dragAlert().dragging;
        savePriceAlertsForScope(dragAlert().instrument_id, dragAlert().timeframe);
        void commitPriceAlertCommand("update", dragAlert());
        refreshPriceAlertLayers();
      };
      canvas.addEventListener("pointermove", onMove);
      canvas.addEventListener("pointerup", onUp);
      canvas.addEventListener("pointercancel", onCancel);
      canvas.addEventListener("lostpointercapture", onCancel);
    }
