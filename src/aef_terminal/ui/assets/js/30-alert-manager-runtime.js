    function setupAlertManager() {
      document.getElementById("alert-add-ema233")?.addEventListener("click", addEma233TouchAlert);
      document.getElementById("alert-add-vsa-fuel")?.addEventListener("click", addVsaFuelAlert);
      document.getElementById("alert-delete-all")?.addEventListener("click", deleteAllPriceAlerts);
      document.getElementById("price-alert-list")?.addEventListener("pointerdown", event => {
        if (event.target?.closest?.("input, select, button")) return;
        const rowId = event.target?.closest?.("[data-alert-id]")?.dataset?.alertId;
        if (rowId) selectPriceAlert(rowId);
      });
      document.getElementById("price-alert-list")?.addEventListener("click", event => {
        if (event.target?.closest?.("input, select")) return;
        const action = event.target?.dataset?.alertAction;
        const id = event.target?.dataset?.alertId;
        if (!action || !id) return;
        const alert = findPriceAlert(id);
        if (!alert) return;
        if (action === "toggle") updatePriceAlert(id, { enabled: !alert.enabled });
        if (action === "rearm") updatePriceAlert(id, { rearm: true });
        if (action === "delete") deletePriceAlert(id);
      });
      document.getElementById("price-alert-list")?.addEventListener("focusout", () => {
        window.setTimeout(() => renderAlertManager({ force: true }), 0);
      });
      document.getElementById("price-alert-list")?.addEventListener("change", event => {
        const action = event.target?.dataset?.alertAction;
        const id = event.target?.dataset?.alertId;
        if (!action || !id) return;
        if (action === "price") updatePriceAlert(id, { price: event.target.value });
        if (action === "direction") updatePriceAlert(id, { direction: event.target.value });
        if (action === "toleranceAtr") updatePriceAlert(id, { toleranceAtr: event.target.value });
      });
    }

    function currentPriceAlerts() {
      return state.alerts.rules.filter(alert => priceAlertMatchesScope(alert, state.instrumentId, state.timeframe));
    }

    function sortedPriceAlertsForManager() {
      const currentInstrumentId = exactIdentityText(state.instrumentId);
      const currentTimeframe = String(state.timeframe || "");
      return [...(state.alerts.rules || [])].sort((left, right) => {
        const leftSymbol = String(left?.symbol || "").toUpperCase();
        const rightSymbol = String(right?.symbol || "").toUpperCase();
        const leftCurrent = priceAlertMatchesScope(left, currentInstrumentId, currentTimeframe) ? 0 : 1;
        const rightCurrent = priceAlertMatchesScope(right, currentInstrumentId, currentTimeframe) ? 0 : 1;
        if (leftCurrent !== rightCurrent) return leftCurrent - rightCurrent;
        if (leftSymbol !== rightSymbol) return leftSymbol.localeCompare(rightSymbol);
        const timeframeCompare = String(left?.timeframe || "").localeCompare(String(right?.timeframe || ""));
        if (timeframeCompare) return timeframeCompare;
        return Number(right?.createdAt || 0) - Number(left?.createdAt || 0);
      });
    }

    function selectedPriceAlert() {
      return state.alerts.rules.find(alert => alert.id === state.alerts.selectedId) || null;
    }

    function selectPriceAlert(id) {
      const alert = findPriceAlert(id);
      if (!alert) return;
      setChartObjectSelection("alert", alert.id);
      renderAlertManager();
      applyDrawingUi();
      if (state.snapshot) requestObjectLayerRedraw("price alert selected");
    }

    function alertStatusText(alert) {
      if (!alert.enabled) return "disabled";
      if (alert.fired) return "fired";
      return alert.armed ? "armed" : "paused";
    }

    function alertDirectionMark(alert) {
      if (alert?.kind === "ema233_touch") return alert.touchDirection === "from_above" ? "EMA↓" : alert.touchDirection === "from_below" ? "EMA↑" : "EMA";
      if (alert?.kind === "vsa_fuel") return "FUEL";
      return alert?.direction === "above" ? "↑" : alert?.direction === "below" ? "↓" : "↕";
    }

    function alertKindText(alert, dynamic) {
      if (alert?.kind === "ema233_touch") {
        const side = alert.touchDirection === "from_above" ? "from above" : alert.touchDirection === "from_below" ? "from below" : "";
        return `EMA 233 touch${side ? ` ${side}` : ""}`;
      }
      if (alert?.kind === "vsa_fuel") return "VSA fuel candle";
      return alert?.direction || "cross";
    }

    function renderTelegramAlertStatus() {
      const node = document.getElementById("telegram-alert-status");
      if (!node) return;
      const telegram = state.systemHealth?.telegram || {};
      if (telegram.configured && telegram.enabled) {
        node.textContent = `Enabled · chat ${telegram.chat_id || "configured"}`;
      } else if (telegram.configured) {
        node.textContent = `Configured · price alerts active · set AEF_TELEGRAM_ENABLED=true for full delivery`;
      } else {
        node.textContent = "Set AEF_TELEGRAM_BOT_TOKEN and AEF_TELEGRAM_CHAT_ID in env or secrets file.";
      }
    }

    function alertManagerHasActiveEditor(list) {
      const active = document.activeElement;
      return Boolean(active && list?.contains(active) && active.matches?.("input, select"));
    }

    function renderAlertManager(options = {}) {
      const list = document.getElementById("price-alert-list");
      const clear = document.getElementById("alert-delete-all");
      if (!list) return;
      if (!options.force && alertManagerHasActiveEditor(list)) return;
      const currentAlerts = currentPriceAlerts();
      const alerts = sortedPriceAlertsForManager();
      if (clear) clear.disabled = currentAlerts.length === 0;
      list.innerHTML = alerts.length
        ? alerts.map(alert => {
          const alertSymbol = String(alert?.symbol || "").toUpperCase();
          const alertTimeframe = String(alert?.timeframe || "");
          const isCurrentScope = priceAlertMatchesScope(alert, state.instrumentId, state.timeframe);
          const dynamic = isCurrentScope ? emaTouchState(alert, state.snapshot) : null;
          const price = Number(dynamic?.level ?? alert.price);
          const isDynamic = Boolean(dynamic) || alert.kind === "ema233_touch" || alert.kind === "vsa_fuel";
          const isEmaTouch = alert.kind === "ema233_touch";
          const toleranceAtr = Number(alert.toleranceAtr ?? 0.08);
          const tolerance = Number(dynamic?.tolerance ?? alert.lastTolerance);
          const toleranceText = Number.isFinite(tolerance) ? ` · tol ${fmt(tolerance)}` : "";
          const rearmText = alert.kind === "ema233_touch" ? ` · rearm ${Number(alert.rearmMinutes || 60)}m` : "";
          const scopeText = `${alertSymbol || "?"} ${alertTimeframe || "?"}`;
          const alertId = escapeHtml(alert.id);
          return `
          <div class="alert-row ${alert.fired ? "fired" : ""} ${alert.id === state.alerts.selectedId ? "active" : ""} ${isCurrentScope ? "current" : "other"}" data-alert-id="${alertId}">
            <input class="settings-control alert-price" name="alert-price-${alertId}" data-alert-action="price" data-alert-id="${alertId}" type="number" step="0.01" value="${Number.isFinite(price) ? price.toFixed(2) : ""}" title="${isDynamic ? "Dynamic EMA 233 level" : "Alert price"}" ${isDynamic ? "disabled" : ""}>
            <select class="settings-control alert-direction" name="alert-direction-${alertId}" data-alert-action="direction" data-alert-id="${alertId}" title="Direction" ${isDynamic ? "disabled" : ""}>
              ${(isDynamic ? [alert.direction || "cross"] : ["cross", "above", "below"]).map(direction => `<option value="${direction}" ${alert.direction === direction ? "selected" : ""}>${direction}</option>`).join("")}
            </select>
            <input class="settings-control alert-tolerance" name="alert-tolerance-${alertId}" data-alert-action="toleranceAtr" data-alert-id="${alertId}" type="number" min="0" max="1" step="0.01" value="${Number.isFinite(toleranceAtr) ? toleranceAtr.toFixed(2) : "0.08"}" title="Tolerance as ATR fraction" ${isEmaTouch ? "" : "disabled"}>
            <div class="alert-status">${escapeHtml(alertDirectionMark(alert))} ${escapeHtml(alertStatusText(alert).toUpperCase())} · ${escapeHtml(alertKindText(alert, dynamic))} · ${escapeHtml(scopeText)}${escapeHtml(toleranceText)}${escapeHtml(rearmText)}</div>
            <div class="alert-actions">
              <button class="tool-button alert-action-button" data-alert-action="delete" data-alert-id="${alertId}" title="Delete alert" aria-label="Delete alert">
                <svg viewBox="0 0 24 24" class="icon" aria-hidden="true" focusable="false"><path d="M2.75 6h18.5M8 6V3.75c0-.9.85-1.65 1.9-1.65h4.2c1.05 0 1.9.75 1.9 1.65V6m3 0-.65 14.15c-.05 1.05-.95 1.85-2 1.85h-8.7c-1.05 0-1.95-.8-2-1.85L5 6m4.75 5.2v6.6m4.5-6.6v6.6"/></svg>
              </button>
              <button class="tool-button alert-action-button" data-alert-action="rearm" data-alert-id="${alertId}" title="Rearm alert" aria-label="Rearm alert">
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" class="icon" fill="none" aria-hidden="true" focusable="false"><path d="M22 2v6h-6" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M21.1 14.5a9.5 9.5 0 1 1-2.43-9.56L22 8" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
            </div>
          </div>
        `;
        }).join("")
        : `<div class="indicator-runtime-note">No price alerts</div>`;
      renderTelegramAlertStatus();
    }
